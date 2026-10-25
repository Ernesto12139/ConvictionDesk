"""Regenerate an ivs-sec capture with acceptance instants verified against EDGAR.

Why this exists: SEC submissions `acceptanceDateTime` normally carries a genuine UTC
instant with a trailing `Z`, but it is not guaranteed to. Some filings are reported to
carry a local (Eastern) clock behind the `Z`. A capture therefore cannot treat the
submissions string as authoritative on its own, and it must never be "corrected" by
subtracting a fixed offset.

What this tool does, per row:

1. keep the raw submissions string verbatim as `raw_submission_acceptance`;
2. read the authoritative Eastern acceptance clock from the EDGAR filing index
   (`Accepted YYYY-MM-DD HH:MM:SS`), which is generated from the SGML
   `<ACCEPTANCE-DATETIME>` header;
3. convert that Eastern clock to UTC with the real timezone rules for that date
   (`America/New_York`, so EDT/EST is decided by the calendar, not by a constant);
4. compare. Only if the submissions instant equals the converted instant is the row
   marked VERIFIED. If they disagree the row keeps `acceptance = None`, so no caller
   can claim second precision or point-in-time eligibility from an ambiguous value.

`raw_metadata_hash` is always computed over the RAW submissions metadata, so a capture
and a live read agree on the same public bytes even when the verified instant differs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from market_agent.sec import (  # noqa: E402
    SEC_WWW,
    _hash,
    filing_metadata_hash,
    filing_source_url,
)

ET = ZoneInfo("America/New_York")
USER_AGENT = "ConvictionDesk capture verifier (audit@example.com)"
REQUEST_INTERVAL = 0.3
READ_RECEIPTS = {}
INDEX_RECEIPTS = {}
_last_request_at = 0.0
VERIFIED = "VERIFIED_INDEX_AND_SUBMISSIONS_AGREE"
DISAGREE = "UNRESOLVED_SUBMISSIONS_DISAGREE"
NO_INDEX = "UNVERIFIED_INDEX_UNAVAILABLE"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get(url: str, *, attempts: int = 3) -> bytes | None:
    global _last_request_at
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.hostname not in {'www.sec.gov','data.sec.gov'}
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError('SEC_METADATA_URL_REFUSED')
    for attempt in range(attempts):
        time.sleep(max(0, REQUEST_INTERVAL-(time.monotonic()-_last_request_at)))
        _last_request_at = time.monotonic()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
                body = response.read()
                READ_RECEIPTS[url] = {'source_uri':url,
                    'source_sha256':'sha256:'+hashlib.sha256(body).hexdigest(),
                    'observed_at':datetime.now(timezone.utc).isoformat()}
                return body
        except urllib.error.HTTPError as exc:
            if exc.code in {403,429}:
                raise SystemExit('SEC_REFUSED_SESSION_STOPPED') from exc
            if 300 <= exc.code < 500:
                return None
            if attempt == attempts - 1:
                return None
        except (urllib.error.URLError, TimeoutError):
            if attempt == attempts - 1:
                return None
            time.sleep(0.5 * (attempt + 1))
    return None


def index_accepted_et(cik: str, accession: str) -> datetime | None:
    """Eastern acceptance clock from the EDGAR filing index page."""
    nodash = accession.replace("-", "")
    url = f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{nodash}/{accession}-index.htm"
    body = _get(url)
    if body is None:
        return None
    text = re.sub(r"<[^>]+>", " ", body.decode("utf-8", "replace"))
    match = re.search(r"Accepted\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})", text)
    if not match:
        return None
    wall = datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}")
    INDEX_RECEIPTS[(cik,accession)] = dict(READ_RECEIPTS[url], accepted_eastern=wall.strftime('%Y-%m-%d %H:%M:%S'))
    return wall


def sgml_accepted_et(cik: str, accession: str) -> datetime | None:
    """SGML <ACCEPTANCE-DATETIME> header, the source the index page is built from."""
    nodash = accession.replace("-", "")
    body = _get(f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{nodash}/{accession}.txt")
    if body is None:
        return None
    match = re.search(r"<ACCEPTANCE-DATETIME>(\d{14})", body[:20000].decode("latin-1", "replace"))
    return datetime.strptime(match.group(1), "%Y%m%d%H%M%S") if match else None


def to_utc(et: datetime) -> datetime:
    """Eastern wall clock to UTC using the calendar, never a fixed offset."""
    eastern = et.replace(tzinfo=ET)
    if (eastern.utcoffset()!=eastern.replace(fold=1).utcoffset()
            or eastern.astimezone(timezone.utc).astimezone(ET).replace(tzinfo=None)!=et):
        raise ValueError('EDGAR_AMBIGUOUS_WALL_TIME')
    return eastern.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def submissions_rows(cik: str) -> tuple[dict[str, dict], int]:
    """Raw submissions metadata keyed by accession, plus SEC's own recent-row count.

    The capture is rebuilt from this, never from the previous fixture: the previous
    fixture's `acceptance` is exactly the value under investigation.
    """
    body = _get(f"https://data.sec.gov/submissions/CIK{cik}.json")
    if body is None:
        raise SystemExit(f"cannot read SEC submissions for CIK {cik}")
    recent = json.loads(body)["filings"]["recent"]
    count = len(recent["accessionNumber"])
    rows = {}
    for index in range(count):
        accession = recent["accessionNumber"][index]
        if accession in rows:
            continue
        rows[accession] = {
            "accession": accession,
            "form": recent["form"][index],
            "base_form": recent["form"][index].split("/")[0],
            "acceptance": recent["acceptanceDateTime"][index],
            "filing_date": recent["filingDate"][index],
            "primary_document": recent["primaryDocument"][index],
            "items": [code for code in (recent["items"][index] or "").split(",") if code],
        }
    return rows, count


def verify_row(cik: str, row: dict) -> dict:
    accession = row["accession"]
    # Archives are keyed by the filer CIK encoded in the accession, which for an
    # agent-filed submission is not the issuer CIK.
    filer_cik = accession.split("-")[0]
    et = index_accepted_et(filer_cik, accession) or index_accepted_et(cik, accession)
    time.sleep(REQUEST_INTERVAL)

    raw = row["acceptance"]
    raw_instant = None
    if isinstance(raw, str) and raw.endswith("Z"):
        try:
            raw_instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raw_instant = None

    out = dict(row)
    out["raw_submission_acceptance"] = raw
    out['verified_edgar_accepted_at_utc'] = None
    out['acceptance_authority'] = INDEX_RECEIPTS.get((filer_cik,accession)) or INDEX_RECEIPTS.get((cik,accession))
    if et is None:
        out["acceptance"] = None
        out["acceptance_verified_from"] = None
        out["time_verification"] = NO_INDEX
        return out

    verified = to_utc(et)
    out["acceptance_verified_from"] = "EDGAR_INDEX_ACCEPTED_ET"
    if raw_instant is not None and raw_instant == verified:
        out["acceptance"] = _iso_utc(verified)
        out['verified_edgar_accepted_at_utc'] = _iso_utc(verified)
        out["time_verification"] = VERIFIED
    else:
        # Ambiguous: keep the raw string, refuse to assert a second-precision instant.
        out["acceptance"] = None
        out["time_verification"] = DISAGREE
    return out


def rebuild_capture(capture: dict, *, verify: bool = True) -> dict:
    cik = capture["issuer"]["cik"]
    source_rows, total = submissions_rows(cik)
    rows = []
    for frozen in capture["rows"]:
        accession = frozen["accession"]
        if accession not in source_rows:
            raise SystemExit(f"{accession} is no longer in SEC's recent list for {cik}")
        row = source_rows[accession]
        # Keep the frozen selection order; take every value from SEC.
        fresh = verify_row(cik, row) if verify else dict(row)
        # The hash binds the raw public metadata, which is what a live read also sees.
        fresh["raw_metadata_hash"] = filing_metadata_hash(
            accession=fresh["accession"], form=fresh["form"],
            acceptance=fresh.get("raw_submission_acceptance", fresh["acceptance"]),
            filing_date=fresh["filing_date"], items=",".join(fresh["items"]),
            primary_document=fresh["primary_document"])
        if fresh.get('acceptance_authority'):
            fresh['acceptance_authority']['source_metadata_hash'] = fresh['raw_metadata_hash']
        rows.append(fresh)

    summary = {
        VERIFIED: sum(1 for row in rows if row.get("time_verification") == VERIFIED),
        DISAGREE: sum(1 for row in rows if row.get("time_verification") == DISAGREE),
        NO_INDEX: sum(1 for row in rows if row.get("time_verification") == NO_INDEX),
    }
    rebuilt = {key: value for key, value in capture.items() if key not in {"rows", "row_count_total"}}
    rebuilt["row_count_total"] = total
    rebuilt["rows"] = rows
    rebuilt["time_verification_summary"] = summary
    rebuilt["time_verification_method"] = (
        "EDGAR filing-index Accepted (Eastern) converted to UTC via America/New_York; "
        "row kept only when the submissions instant matches.")
    rebuilt["raw_metadata_hash"] = _hash(rows)
    rebuilt['source_metadata_retrieved_at'] = READ_RECEIPTS[f'https://data.sec.gov/submissions/CIK{cik}.json']['observed_at']
    return rebuilt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", default="evals/fixtures/ivs_sec")
    parser.add_argument("--out", default=None, help="write here instead of in place")
    parser.add_argument("--no-verify", action="store_true",
                        help="fetch raw submissions without index verification; no verified-time claim")
    args = parser.parse_args()

    folder = Path(args.fixtures)
    target = Path(args.out) if args.out else folder
    target.mkdir(parents=True, exist_ok=True)

    for path in sorted(folder.glob("*.json")):
        capture = json.loads(path.read_text(encoding="utf-8"))
        rebuilt = rebuild_capture(capture, verify=not args.no_verify)
        destination = target / path.name
        destination.write_text(json.dumps(rebuilt, ensure_ascii=False, indent=1) + "\n",
                               encoding="utf-8")
        print(f"{path.name}: {rebuilt['time_verification_summary']} -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
