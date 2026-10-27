"""Extract one real Owner holding value slice from a sandboxed DB copy.

Safety: refuses the canonical live Owner database path, opens SQLite read-only,
uses only local capture files and never performs a network or model request.

Example (run from the repository root, with the candidate ``src`` first on
``PYTHONPATH``)::

    python scripts/extract_owner_value_003.py \
        --database "$TEMP/mia-owner-copy.db" --account-id a0 --symbol GLXY \
        --sec-capture evals/fixtures/owner_value_003/sec/GLXY_capture.json \
        --whatif-side BUY --whatif-shares 5 --whatif-price 25.00 \
        --whatif-rationale "Illustrative add sizing only" --output slice.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from market_agent.domain import PortfolioSnapshot, utc
from market_agent.owner_value_slice import (SEC_ACCEPTED_RIGHTS, SEC_BODY_RIGHTS,
                                            PriceObservationInput, SecBodyChunk,
                                            SecEvent, SecFilingBody, ThesisItem,
                                            ThesisRecord, WhatIfRequest,
                                            build_owner_value_slice)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / 'data' / 'market_agent.db'
THESIS_SCAN_LIMIT = 50


def _git_common_root(start: Path) -> Path | None:
    """Repository root shared by every worktree of this checkout, if any.

    A linked worktree reports the main checkout's ``.git`` as its common dir, so
    this finds the canonical root even when the script runs from an isolated
    acceleration worktree. Failure is not fatal: the guard then relies on the
    script's own checkout and ``MIA_DB_PATH``.
    """
    try:
        done = subprocess.run(['git', '-C', str(start), 'rev-parse', '--git-common-dir'],
                              capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    value = done.stdout.strip()
    if done.returncode != 0 or not value:
        return None
    common = Path(value)
    if not common.is_absolute():
        common = (start / common).resolve()
    return common.parent if common.name == '.git' else common


def live_database_candidates() -> tuple[Path, ...]:
    """Every path that would be the live Owner database for this machine.

    The script's own checkout is one candidate, never the only one: the runtime
    may be configured through ``MIA_DB_PATH`` and the canonical checkout may be
    a different directory than this worktree.
    """
    candidates = [DEFAULT_DATABASE]
    configured = os.environ.get('MIA_DB_PATH')
    if configured:
        raw = Path(configured).expanduser()
        candidates.append(raw)
        if not raw.is_absolute():
            candidates.append(ROOT / raw)
    common_root = _git_common_root(ROOT)
    if common_root is not None:
        candidates.append(common_root / 'data' / 'market_agent.db')
    return tuple(candidates)


def is_live_database(path: Path) -> bool:
    """True when ``path`` is the live Owner DB, including links and hardlinks."""
    requested = path.expanduser().resolve()
    for candidate in live_database_candidates():
        resolved = candidate.expanduser().resolve()
        if requested == resolved:
            return True
        if resolved.exists() and requested.exists() and os.path.samefile(requested, resolved):
            return True
    return False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return 'sha256:' + digest.hexdigest()


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise SystemExit('--evaluation-time must include a timezone')
    return parsed.astimezone(timezone.utc)


def open_read_only(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def load_snapshot(connection, snapshot_id: str | None) -> PortfolioSnapshot:
    if snapshot_id:
        row = connection.execute(
            'select payload from portfolio_snapshots where snapshot_id=?', (snapshot_id,)
        ).fetchone()
    else:
        row = connection.execute(
            'select payload from portfolio_snapshots order by as_of desc limit 1'
        ).fetchone()
    if row is None:
        raise SystemExit('NO_PORTFOLIO_SNAPSHOT')
    return PortfolioSnapshot.model_validate(json.loads(row[0]))


def load_thesis(connection, symbol: str,
                boundary: datetime) -> tuple[ThesisRecord | None, dict]:
    """Latest version that existed *and was stored* strictly before the boundary.

    The scan runs newest first but keeps going: a newer backdated version that was
    recorded after the event must not hide an older version the Owner really had.
    """
    rows = connection.execute(
        'select thesis_version_id, symbol, as_of, status, evidence_id, payload, evidence_payload '
        'from thesis_versions where symbol=? and as_of < ? '
        'order by as_of desc, rowid desc limit ?',
        (symbol, utc(boundary).isoformat(), THESIS_SCAN_LIMIT)).fetchall()
    rejected_after_boundary = 0
    availability_unknown = 0
    for row in rows:
        try:
            as_of = utc(datetime.fromisoformat(str(row[2]).replace('Z', '+00:00')))
        except ValueError:
            continue
        if as_of >= boundary:
            continue
        evidence = json.loads(row[6] or '{}')
        stored = evidence.get('retrieved_at')
        stored_available_at = None
        if stored:
            try:
                stored_available_at = utc(
                    datetime.fromisoformat(str(stored).replace('Z', '+00:00')))
            except ValueError:
                stored_available_at = None
        if stored_available_at is None:
            availability_unknown += 1
            continue
        if stored_available_at >= boundary:
            rejected_after_boundary += 1
            continue
        payload = json.loads(row[5])
        sections = {}
        for key in ('core_drivers', 'key_assumptions', 'execution_risks',
                    'invalidation_conditions'):
            sections[key] = tuple(ThesisItem(text=item['text'],
                                             evidence_ids=tuple(item.get('evidence_ids') or ()))
                                  for item in payload.get(key) or ())
        record = ThesisRecord(
            thesis_version_id=row[0], symbol=row[1], as_of=as_of, status=row[3],
            evidence_id=evidence.get('evidence_id') or row[4],
            stored_available_at=stored_available_at, **sections)
        return record, {'scanned': len(rows), 'selection': 'STRICT_VERSION_SCAN',
                        'rejected_after_boundary': rejected_after_boundary,
                        'availability_unknown': availability_unknown,
                        'thesis_version_id': record.thesis_version_id}
    reason = ('NO_VERSIONS_BEFORE_BOUNDARY' if not rows
              else 'RECORDED_AFTER_BOUNDARY' if rejected_after_boundary
              else 'STORED_AVAILABILITY_UNKNOWN')
    return None, {'scanned': len(rows), 'selection': 'STRICT_VERSION_SCAN',
                  'rejected_after_boundary': rejected_after_boundary,
                  'availability_unknown': availability_unknown,
                  'thesis_version_id': None, 'reason': reason}


def load_price(connection, symbol: str) -> PriceObservationInput | None:
    row = connection.execute(
        'select payload from provider_cache where symbol=? '
        'order by observation_as_of desc limit 1', (symbol,)
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row[0])
    observations = payload.get('observations') or []
    evidence = (payload.get('evidence') or [{}])[0]
    if not observations:
        return None
    observation = observations[0]
    structured = evidence.get('structured_data') or {}
    basis = {'unadjusted': 'UNADJUSTED', 'adjusted': 'ADJUSTED'}.get(
        str(structured.get('price_basis', 'unknown')).lower(), 'UNKNOWN')
    return PriceObservationInput(
        symbol=observation['symbol'], price=observation['price'],
        currency=observation.get('currency', 'USD'), provider=observation.get('provider',
                                                                            evidence.get('provider', 'UNKNOWN')),
        observed_at=observation['as_of'], retrieved_at=evidence.get('retrieved_at')
        or observation['as_of'], price_basis=basis,
        usage_rights='UNKNOWN', source_uri=evidence.get('source_uri') or 'UNKNOWN',
        evidence_id=observation.get('evidence_id') or evidence.get('evidence_id', 'UNKNOWN'),
        delay_note='PROVIDER_DELAY_AND_RIGHTS_NOT_RECORDED')


def load_event(capture: Path, accession: str | None) -> SecEvent:
    document = json.loads(capture.read_text(encoding='utf-8'))
    rows = document.get('rows') or []
    if accession:
        row = next((item for item in rows if item.get('accession') == accession), None)
    else:
        row = next((item for item in rows
                    if item.get('form') == '8-K' and item.get('items')), None)
    if row is None:
        raise SystemExit('NO_ELIGIBLE_SEC_EVENT_IN_CAPTURE')
    authority = row.get('acceptance_authority') or {}
    return SecEvent(
        symbol=document['issuer']['symbol'], cik=document['issuer']['cik'],
        accession=row['accession'], form=row['form'], items=tuple(row.get('items') or ()),
        accepted_at=row.get('verified_edgar_accepted_at_utc') or row['acceptance'],
        captured_at=authority.get('observed_at') or document['captured_at'],
        source_uri=authority.get('source_uri')
        or f"https://www.sec.gov/Archives/edgar/data/{int(document['issuer']['cik'])}/"
           f"{row['accession'].replace('-', '')}/{row['accession']}-index.htm",
        source_hash=authority.get('source_sha256') or row['raw_metadata_hash'],
        rights=SEC_ACCEPTED_RIGHTS)


def load_filing_body(connection, accession: str) -> SecFilingBody | None:
    tables = {row[0] for row in connection.execute(
        "select name from sqlite_master where type='table'")}
    if not {'sec_documents', 'sec_chunks'} <= tables:
        return None
    document = connection.execute(
        'select document_id, cik, form, filed_at, retrieved_at, source_uri, content_hash '
        'from sec_documents where accession_number=? order by retrieved_at desc limit 1',
        (accession,)).fetchone()
    if document is None:
        return None
    chunks = connection.execute(
        'select sequence, section, text, content_hash from sec_chunks '
        'where document_id=? order by sequence', (document['document_id'],)).fetchall()
    if not chunks:
        return None
    return SecFilingBody(
        accession=accession, cik=document['cik'], document_id=document['document_id'],
        form=document['form'], filed_at=document['filed_at'],
        retrieved_at=document['retrieved_at'], source_uri=document['source_uri'],
        content_hash=document['content_hash'], rights=SEC_BODY_RIGHTS,
        chunks=tuple(SecBodyChunk(sequence=chunk['sequence'], section=chunk['section'],
                                  text=chunk['text'], content_hash=chunk['content_hash'])
                     for chunk in chunks))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--database', required=True, type=Path,
                        help='Sandboxed copy of the Owner DB. The live DB path is refused.')
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--snapshot-id')
    parser.add_argument('--sec-capture', required=True, type=Path)
    parser.add_argument('--accession')
    parser.add_argument('--sec-body', choices=('auto', 'none'), default='auto',
                        help='Load the captured SEC filing body from the DB copy when present.')
    parser.add_argument('--evaluation-time', default='now')
    parser.add_argument('--local-full', action='store_true',
                        help='Include unpriced symbols in local output.')
    parser.add_argument('--whatif-side', choices=('BUY', 'SELL'))
    parser.add_argument('--whatif-shares')
    parser.add_argument('--whatif-price')
    parser.add_argument('--whatif-fee', default='0')
    parser.add_argument('--whatif-rationale')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)

    database = args.database.expanduser().resolve()
    if is_live_database(database):
        print('LIVE_DATABASE_REFUSED_COPY_REQUIRED', file=sys.stderr)
        return 2
    if not database.is_file():
        print('DATABASE_COPY_NOT_FOUND', file=sys.stderr)
        return 2
    capture = args.sec_capture.resolve()
    if not capture.is_file():
        print('SEC_CAPTURE_NOT_FOUND', file=sys.stderr)
        return 2
    evaluation_time = (datetime.now(timezone.utc) if args.evaluation_time == 'now'
                       else parse_time(args.evaluation_time))
    event = load_event(capture, args.accession)
    # A thesis is prior knowledge only before both the filing and this evaluation.
    thesis_boundary = evaluation_time
    if event.accepted_at < thesis_boundary:
        thesis_boundary = event.accepted_at

    connection = open_read_only(database)
    try:
        snapshot = load_snapshot(connection, args.snapshot_id)
        thesis, thesis_selection = load_thesis(connection, args.symbol,
                                               thesis_boundary)
        price = load_price(connection, args.symbol)
        filing_body = (load_filing_body(connection, event.accession)
                       if args.sec_body == 'auto' else None)
    finally:
        connection.close()

    whatif = None
    if args.whatif_side or args.whatif_shares or args.whatif_price:
        missing = [name for name, value in (('--whatif-side', args.whatif_side),
                                            ('--whatif-shares', args.whatif_shares),
                                            ('--whatif-price', args.whatif_price),
                                            ('--whatif-rationale', args.whatif_rationale))
                   if not value]
        if missing:
            print('MISSING_WHATIF_INPUTS:' + ','.join(missing), file=sys.stderr)
            return 2
        whatif = WhatIfRequest(side=args.whatif_side, shares=args.whatif_shares,
                               price_basis='HYPOTHETICAL_OPERATOR_INPUT',
                               price=args.whatif_price, fee=args.whatif_fee,
                               rationale=args.whatif_rationale)

    slice_receipt = build_owner_value_slice(
        snapshot=snapshot, account_id=args.account_id, symbol=args.symbol,
        evaluation_time=evaluation_time, event=event, filing_body=filing_body,
        price=price, thesis=thesis, whatif=whatif,
        redaction='LOCAL_FULL' if args.local_full else 'SHAREABLE_REDACTED')
    output = {
        'extraction': {
            'database_sha256': sha256_file(database),
            'thesis_selection': thesis_selection,
            'thesis_boundary': thesis_boundary.isoformat(),
            'database_is_live_owner_db': False,
            'sec_capture_sha256': sha256_file(capture),
            'sec_capture_path': capture.name,
            'sec_body_document_id': (filing_body.document_id if filing_body else None),
            'sec_body_chunks': (len(filing_body.chunks) if filing_body else 0),
            'sec_body_status': slice_receipt.filing_body.status,
            'evaluation_time': evaluation_time.isoformat(),
            'network_calls': 0,
            'model_calls': 0,
            'database_writes': 0,
        },
        'slice': slice_receipt.model_dump(mode='json'),
    }
    text = json.dumps(output, indent=2, sort_keys=True, ensure_ascii=False) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding='utf-8', newline='\n')
        print(f"WROTE {args.output} {slice_receipt.content_hash}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
