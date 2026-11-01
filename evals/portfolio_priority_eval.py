"""Frozen public/synthetic outcome checks against a running HTTP assembly.

This is a deterministic product proxy, never a model-quality or Owner verdict.
"""
import time
from decimal import Decimal


def run(client):
    checks = []
    def check(name, condition):
        assert condition, name
        checks.append({'case': name, 'verdict': 'PASS'})
    def finish(response):
        check('typed_research_accepted', response.status_code == 202)
        tid = response.json()['task_id']
        until = time.monotonic() + 30
        while time.monotonic() < until:
            result = client.get('/api/research/tasks/' + tid).json()
            if result['status'] != 'RUNNING':
                check('scripted_research_saved', result['status'] == 'COMPLETE')
                return result
            time.sleep(.05)
        raise AssertionError('RESEARCH_DEADLINE')
    overview = client.get('/api/portfolio-priority').json()
    check('four_accounts_no_current_quote', len(overview['accounts']) == 4 and overview['coverage']['current_quote_symbols'] == 0)
    check('j2_rank_all_holdings', [c['symbol'] for c in overview['candidates']] == ['APP','UNP','AAPL','DOW','NVDA','KR'])
    check('unknown_price_not_zero', overview['accounts'][2]['synthetic_nav'] is None and overview['coverage']['missing_price_symbols'] == ['AAPL'])
    body = {'symbol': 'APP', 'account_id': 'SYN-GROWTH', 'mode': 'FAST'}
    fast = finish(client.post('/api/portfolio-priority/research', json=body))
    tid = fast['task_id']
    url = '/api/research/tasks/' + tid
    frozen = client.get(url + '/revisions/1').content
    check('no_prior_is_explicit', client.get('/api/portfolio-priority/tasks/' + tid + '/delta').json()['prior_thesis'] == 'NO_CONFIRMED_PRIOR_THESIS')
    deep = finish(client.post('/api/portfolio-priority/research', json={**body,
        'mode': 'DEEP', 'task_id': tid, 'expected_revision': 1}))
    delta = client.get('/api/portfolio-priority/tasks/' + tid + '/delta').json()
    check('deep_real_information_gain', delta['information_gain'] == 'NEW_PUBLIC_SEC_FACTS' and len(delta['new_evidence']) == 1)
    fact = delta['new_evidence'][0]['structured_data']
    check('same_accession_period_unit', len({f['accession_number'] for f in fact['metrics']}) == 1 and {f['unit'] for f in fact['metrics']} == {'USD'})
    check('decimal_comparison_not_quarter_ytd_mix', fact['comparison']['revenue']['yoy_pct'] == '55.77' and fact['comparison']['operating_cash_flow']['yoy_pct'] == '34.70')
    check('insufficient_is_not_hold', deep['action_decision']['decision_status'] == 'UNDETERMINED' and deep['action_decision']['decision_action'] is None)
    before = client.get('/api/portfolio/current').content
    fill = {'revision': 2, 'symbol': 'APP', 'account_id': 'SYN-GROWTH', 'side': 'BUY',
        'shares': '5', 'fill_price': '200', 'fee': '1', 'other_prices': {'UNP': '240'},
        'scenario_date': '2026-10-30', 'unchanged_share_basis_attested': True}
    response = client.post(url + '/what-if', json=fill)
    check('whatif_accepted', response.status_code == 200)
    receipt = response.json()
    check('exact_account_cash_shares_nav', receipt['after']['cash'] == '199' and receipt['after']['shares'] == '35' and receipt['after']['nav'] == '8399')
    check('concentration_increases', Decimal(receipt['after']['focus_weight']) > Decimal(receipt['before']['focus_weight']))
    check('no_pooling_account_cash', client.post(url + '/what-if', json={**fill, 'shares': '7'}).status_code == 422)
    check('no_foreign_account', client.post(url + '/what-if', json={**fill, 'account_id': 'SYN-RESERVE'}).status_code == 422)
    check('no_portfolio_or_history_mutation', client.get('/api/portfolio/current').content == before and client.get(url + '/revisions/1').content == frozen)
    trace = client.get(url + '/execution-traces').json()
    check('actual_trace_bound', len(trace['receipts']) >= 3 and all(r['trace']['execution_mode'] == 'KEYLESS_SCRIPTED' for r in trace['receipts']))
    check('orders_and_generic_dispatch_blocked', client.post('/api/query', json={}).status_code == 403 and client.post('/api/orders', json={}).status_code == 403)
    check('unknown_task_is_typed_404', client.post('/api/research/tasks/missing/what-if', json=fill).status_code == 404)
    return {'schema_version': 'portfolio-priority-eval-v1', 'verdict': 'PASS', 'scope': 'PUBLIC_SYNTHETIC_SCRIPTED_HTTP',
        'checks': checks, 'task_id': tid, 'revision_one': fast, 'revision_two': deep,
        'receipt': receipt, 'coverage': overview['coverage'], 'paid_calls': 0, 'owner_acceptance': 'NOT_ASSESSED'}


if __name__ == '__main__':
    import json
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from fastapi.testclient import TestClient
    from market_agent.serve import build_public_workbench
    with TemporaryDirectory(prefix='public-priority-eval-') as folder:
        app = build_public_workbench(Path(folder) / 'replay')
        with TestClient(app, base_url='http://127.0.0.1:8080', client=('127.0.0.1', 1)) as client:
            assert 'href="/portfolio-priority"' in client.get('/').text
            assert client.get('/api/public-demo').status_code == 200
            assert client.get('/portfolio-priority').status_code == 200
            result = run(client)
            assert client.get('/public-replay').status_code == 200
            assert client.get('/api/public-demo').status_code == 200
            print(json.dumps({'verdict': result['verdict'], 'checks': len(result['checks']),
                'entrypoint': 'DEFAULT_PUBLIC_SERVER', 'paid_calls': result['paid_calls']}))
