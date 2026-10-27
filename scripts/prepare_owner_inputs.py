"""Build one private input bundle from an explicitly named read-only DB copy."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from extract_owner_value_003 import (is_live_database, open_read_only, load_snapshot,
    load_thesis, load_price, load_event, load_filing_body)
from market_agent.owner_workbench import OwnerInputs
from market_agent.tdb import content_hash


def prepare(database, capture, account_id, symbol, output, *, live_database):
    database, output = Path(database).resolve(strict=True), Path(output).absolute()
    live_database = Path(live_database).resolve(strict=True)
    if is_live_database(database) or os.path.samefile(database, live_database):
        raise ValueError('LIVE_DATABASE_REFUSED_COPY_REQUIRED')
    root = Path(__file__).resolve().parents[1]
    for forbidden in ('src','tests','evals','release','docs'):
        if output.resolve().is_relative_to(root/forbidden):
            raise ValueError('PRIVATE_INPUTS_IN_SOURCE_REFUSED')
    if output.exists() or output.resolve() == database:
        raise ValueError('NEW_PRIVATE_OUTPUT_REQUIRED')
    event = load_event(Path(capture).resolve(strict=True), None)
    if event.symbol != symbol:
        raise ValueError('CAPTURE_SYMBOL_MISMATCH')
    now = datetime.now(timezone.utc)
    with open_read_only(database) as connection:
        snapshot = load_snapshot(connection, None)
        if not any(p.account_id==account_id and p.symbol==symbol for p in snapshot.positions):
            raise ValueError('HOLDING_SCOPE_NOT_PRESENT')
        thesis, _ = load_thesis(connection, symbol, min(now,event.accepted_at))
        inputs = OwnerInputs(snapshot_id=snapshot.snapshot_id,
            snapshot_hash=content_hash(snapshot.model_dump(mode='json')), account_id=account_id,
            symbol=symbol, event=event, filing_body=load_filing_body(connection,event.accession),
            price=load_price(connection,symbol), thesis=thesis)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x',encoding='utf-8') as stream:
        stream.write(inputs.model_dump_json(indent=2))
    return {'status':'PRIVATE_INPUTS_PREPARED','owner_database_written':False,
            'model_calls':0,'provider_calls':0,'snapshot_bound':True}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--live-database',type=Path,required=True,
                        help='Explicit protected original, including when it lives in another repository clone.')
    parser.add_argument('--sec-capture',type=Path,required=True)
    parser.add_argument('--account-id',required=True)
    parser.add_argument('--symbol',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(prepare(args.database,args.sec_capture,args.account_id,args.symbol,args.output,
                             live_database=args.live_database)))
