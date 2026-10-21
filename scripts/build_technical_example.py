"""Build owned synthetic regular-session OHLCV; never a market-data downloader."""
from datetime import date,datetime,timedelta,time
from pathlib import Path
from zoneinfo import ZoneInfo
import json

def example(symbol='EXAMPLE'):
    day=date(2026,9,18);bars=[]
    while day<=date(2026,10,16):
        if day.weekday()<5:
            value=100+len(bars)
            bars.append(dict(session_date=day.isoformat(),available_at=datetime.combine(day,time(16),ZoneInfo('America/New_York')).isoformat(),
                open=str(value),high=str(value+2),low=str(value-2),close=str(value),volume=100000))
        day+=timedelta(days=1)
    return dict(symbol=symbol,currency='USD',identity='SYNTHETIC',source_id='owned-linear-ohlcv-20261017-v1',
        source_note='Owned synthetic teaching series; not prices or events for this ticker.',usage_rights='SYNTHETIC_OWNED',
        retrieved_at='2026-10-17T12:00:00Z',session='REGULAR',adjustment='UNADJUSTED',
        adjustment_as_of='2026-10-17T12:00:00Z',adjustment_lineage='Synthetic native unadjusted OHLC, no actions in constructed series.',
        split_status='NONE_CONFIRMED',halt_status='NONE_CONFIRMED',share_class='SINGLE_COMMON',bars=bars)

if __name__=='__main__':
    path=Path(__file__).resolve().parents[1]/'src/market_agent/demo_assets/technical-example-v1.json'
    path.write_text(json.dumps(example(),indent=2)+'\n',encoding='utf-8')
