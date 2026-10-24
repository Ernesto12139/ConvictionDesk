"""Offline SEC metadata projection: real captures, synthetic scopes, no model."""
from datetime import datetime, timedelta
import json
from pathlib import Path
from market_agent.sec_filings import CapturedFilingsSource, project


def main():
    folder = Path(__file__).resolve().parents[1] / 'evals/fixtures/ivs_sec'
    captures = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(folder.glob('*.json'))]
    sources = {c['issuer']['symbol']: CapturedFilingsSource(c) for c in captures}
    as_of = max(datetime.fromisoformat(c['captured_at']) for c in captures) + timedelta(seconds=1)

    class FrozenSEC:
        def recent_filings(self, symbol, *, as_of):
            return sources[symbol].recent_filings(symbol, as_of=as_of)

    result = project(FrozenSEC(), as_of=as_of,
                     scopes=[('UNP', 'HOLDING'), ('KR', 'HOLDING'), ('DOW', 'WATCHING')],
                     capture_identity='public-sec-metadata-frozen-capture')
    print(json.dumps({'scope': 'REAL_PUBLIC_SEC_METADATA_SYNTHETIC_SCOPE',
                      'live_network': False, 'model_calls': 0,
                      'projection': result.model_dump(mode='json')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
