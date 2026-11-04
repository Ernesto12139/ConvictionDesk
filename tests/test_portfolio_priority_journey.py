"""The whole new journey and refusals; no real accounts, providers or credentials."""
import copy
import json
import socket
import subprocess
import sys
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from market_agent import portfolio_priority_journey as journey
from market_agent.portfolio_entry import PortfolioEntry
from market_agent.tdb import content_hash

BASE = 'http://127.0.0.1:8793'


@pytest.fixture(autouse=True)
def no_external(monkeypatch):
    original = socket.socket.connect
    def guarded(sock, address):
        if isinstance(address, tuple) and address[0] not in {'localhost', '127.0.0.1', '::1'}:
            raise AssertionError('EXTERNAL_NETWORK_FORBIDDEN')
        return original(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', guarded)


def finish(client, response):
    assert response.status_code == 202, response.text
    tid = response.json()['task_id']
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        task = client.get('/api/research/tasks/' + tid).json()
        if task['status'] != 'RUNNING':
            return task
        time.sleep(.02)
    raise AssertionError('SCRIPTED_TASK_DEADLINE')


def research(client, **updates):
    return finish(client, client.post('/api/portfolio-priority/research', json={
        'symbol': 'APP', 'account_id': 'SYN-GROWTH', **updates}))


def fill(task, **updates):
    return {'revision': task['revision'], 'account_id': 'SYN-GROWTH', 'symbol': 'APP',
        'side': 'BUY', 'shares': '5', 'fill_price': '200', 'fee': '1',
        'scenario_date': '2026-10-30', 'other_prices': {'UNP': '240'},
        'unchanged_share_basis_attested': True, **updates}


def test_full_fast_deep_whatif_history_and_restart(tmp_path):
    folder = tmp_path / 'isolated'
    app = journey.build_app(folder)
    with TestClient(app, base_url=BASE, client=('127.0.0.1', 45001)) as client:
        overview = client.get('/api/portfolio-priority').json()
        assert overview['coverage'] == dict(held_symbols=6, held_positions=8,
            synthetic_priced_symbols=5, sec_metadata_symbols=4, comparable_financial_symbols=1,
            current_quote_symbols=0, missing_price_symbols=['AAPL'])
        assert [c['symbol'] for c in overview['candidates']] == ['APP', 'UNP', 'AAPL', 'DOW', 'NVDA', 'KR']
        assert overview['candidates'][0]['committed_cost'] == '5200'
        assert overview['accounts'][2]['synthetic_nav'] is None
        assert all(a['current_nav'] is None for a in overview['accounts'])
        fast = research(client)
        assert fast['status'] == 'COMPLETE', fast.get('error')
        assert fast['account_id'] == 'SYN-GROWTH'
        assert fast['action_decision']['decision_status'] == 'UNDETERMINED'
        assert fast['action_decision']['decision_action'] is None
        tid = fast['task_id']
        revision_url = f'/api/research/tasks/{tid}/revisions/1'
        frozen = client.get(revision_url).content
        deep = research(client, task_id=tid, expected_revision=1, mode='DEEP')
        assert deep['status'] == 'COMPLETE', deep.get('error')
        assert deep['revision'] == 2
        assert 'sec.fundamentals' not in [c['name'] for c in fast['tool_calls']]
        assert 'sec.fundamentals' in [c['name'] for c in deep['tool_calls']]
        delta = client.get(f'/api/portfolio-priority/tasks/{tid}/delta').json()
        assert delta['information_gain'] == 'NEW_PUBLIC_SEC_FACTS'
        assert delta['prior_thesis'] == 'NO_CONFIRMED_PRIOR_THESIS'
        e = delta['new_evidence'][0]
        assert content_hash(e['structured_data']) == e['content_hash']
        assert e['source_record_id'] == '0001751008-26-000059'
        assert e['structured_data']['comparison']['revenue']['yoy_pct'] == '55.77'
        assert e['structured_data']['comparison']['operating_cash_flow']['yoy_pct'] == '34.70'
        ids = {s['evidence']['evidence_id'] for s in deep['sources']}
        for group in ('key_reasons', 'support', 'counterevidence', 'next_checks'):
            assert deep['assessment'][group]
            assert all(set(s['evidence_ids']) <= ids for s in deep['assessment'][group])
        route = f'/api/research/tasks/{tid}/what-if'
        before = client.get('/api/portfolio/current').content
        receipt = client.post(route, json=fill(deep)).json()
        assert receipt['before']['cash'] == '1200'
        assert receipt['after']['cash'] == '199'
        assert receipt['before']['shares'] == '30'
        assert receipt['after']['shares'] == '35'
        assert receipt['before']['nav'] == '8400'
        assert receipt['after']['nav'] == '8399'
        assert Decimal(receipt['after']['focus_weight']).quantize(Decimal('.000001')) == (Decimal(7000) / Decimal(8399)).quantize(Decimal('.000001'))
        assert client.get('/api/portfolio/current').content == before
        assert client.get(revision_url).content == frozen
        historical_delta = client.get(f'/api/portfolio-priority/tasks/{tid}/delta?revision=1').json()
        assert historical_delta['revision'] == 1 and historical_delta['prior_decision'] == 'NO_PRIOR_VERSION'
        assert historical_delta['new_evidence'] == []
        # Cash from the reserve account is never admitted for the growth account.
        bad = client.post(route, json=fill(deep, shares='7'))
        assert bad.status_code == 422 and bad.json()['detail'] == 'INSUFFICIENT_CASH_INCLUDING_FEES'
        assert client.post(route, json=fill(deep, account_id='SYN-RESERVE')).status_code == 422
        assert client.post(route, json=fill(deep, side='SELL', shares='31')).status_code == 422
        assert client.post(route, json=fill(deep, fill_price=200.01)).status_code == 422
        assert client.post(route, json=fill(deep, fill_price='NaN')).status_code == 422
        assert client.post(route, json=fill(deep, unchanged_share_basis_attested=False)).status_code == 422
        traces = client.get(f'/api/research/tasks/{tid}/execution-traces').json()['receipts']
        assert len(traces) >= 3
        assert {r['trace']['revision_id'] for r in traces} == {'1', '2'}
        assert all(r['trace']['task_id'] == tid for r in traces)
        assert all(r['trace']['execution_mode'] == 'KEYLESS_SCRIPTED' for r in traces)
        assert 'SCRIPTED' in json.dumps(traces)
        assert 'REAL_MODEL' not in json.dumps(traces)
        assert client.post('/api/portfolio-priority/research', json={
            'symbol': 'APP', 'account_id': 'SYN-GROWTH', 'mode': 'DEEP',
            'task_id': tid, 'expected_revision': 1}).status_code == 409
    restarted = journey.build_app(folder)
    with TestClient(restarted, base_url=BASE, client=('127.0.0.1', 45002)) as client:
        assert client.get(revision_url).content == frozen
        assert client.get('/api/research/tasks/' + tid).json()['revision'] == 2
        assert client.get('/api/portfolio-priority').json() == overview


@pytest.mark.parametrize('change,code', [
    (lambda d: d['financial_capture']['facts'][0].update(unit='shares'), 'PERIOD_UNIT_OR_PIT'),
    (lambda d: d['financial_capture']['facts'][0].update(accession_number='0000000000-00-000000'), 'ACCESSION'),
    (lambda d: d['financial_capture'].update(cik='0000000000'), 'IDENTITY'),
    (lambda d: d['financial_capture'].update(rights='LICENSED_NEWS'), 'RIGHTS'),
    (lambda d: d['financial_capture'].update(captured_at='2099-01-01T00:00:00Z'), 'AFTER_CUTOFF'),
    (lambda d: d['financial_capture']['facts'][0].update(start='2025-04-01'), 'COMPARABLE_PERIOD'),
    (lambda d: d['financial_capture']['facts'][0].update(available_at='2026-08-05T21:01:19Z'), 'PIT'),
    (lambda d: d['captures'][0]['rows'][0].update(form='10-K'), 'METADATA_INTEGRITY'),
])
def test_source_period_identity_and_pit_fail_closed(change, code):
    data = copy.deepcopy(journey.load_dataset())
    change(data)
    with pytest.raises(ValueError, match=code):
        journey.financial_packet(data, journey.instant(data['as_of']))


def test_ranking_is_order_independent_and_unknown_never_zero():
    data = journey.load_dataset()
    entry = PortfolioEntry(accounts=data['accounts'], positions=data['positions'])
    snapshot = entry.snapshot(journey.instant(data['snapshot_as_of']))
    a = journey.priority(snapshot, data)
    reversed_snapshot = snapshot.model_copy(update={'positions': tuple(reversed(snapshot.positions))})
    b = journey.priority(reversed_snapshot, data)
    assert a['candidates'] == b['candidates']
    aapl = next(p for acc in a['accounts'] for p in acc['positions'] if p['symbol'] == 'AAPL')
    assert aapl['synthetic_value'] is None and aapl['synthetic_mark'] is None
    assert next(c for c in a['candidates'] if c['symbol'] == 'AAPL')['rank'] == 3


def test_boundary_and_degraded_source_are_explicit(tmp_path):
    app = journey.build_app(tmp_path / 'public')
    with TestClient(app, base_url=BASE, client=('127.0.0.1', 45003)) as c:
        for path in ('/api/query', '/api/research/tasks', '/api/portfolio/entry', '/api/providers/OpenAI/verify'):
            assert c.post(path, json={}).status_code == 403
        for payload, code in [
            ({'symbol': 'APP', 'account_id': 'SYN-CORE'}, 'SECURITY_NOT_HELD'),
            ({'symbol': 'AAPL', 'account_id': 'SYN-CORE'}, 'PUBLIC_SEC_NOT_COVERED'),
            ({'symbol': 'APP', 'account_id': 'SYN-GROWTH', 'mode': 'DEEP'}, 'START_WITH_FAST')]:
            response = c.post('/api/portfolio-priority/research', json=payload)
            assert response.status_code == 422 and code in response.json()['detail']
        body = {'symbol': 'APP', 'account_id': 'SYN-GROWTH'}
        assert c.post('/api/portfolio-priority/research', json=body, headers={'Origin': 'https://hostile.example'}).status_code == 403
        assert c.post('/api/portfolio-priority/research', json=body, headers={'X-Forwarded-For': '127.0.0.1'}).status_code == 403
        assert c.get('/api/research/tasks/no-such-task').status_code == 404
        assert c.get('/api/portfolio-priority/tasks/no-such-task/delta').status_code == 404
        assert c.post('/api/research/tasks/no-such-task/what-if', json=fill({'revision': 1})).status_code == 404
        fast = research(c, symbol='UNP')
        assert fast['status'] == 'COMPLETE'
        deep = research(c, symbol='UNP', task_id=fast['task_id'], mode='DEEP', expected_revision=1)
        assert deep['status'] == 'COMPLETE'
        delta = c.get(f"/api/portfolio-priority/tasks/{deep['task_id']}/delta").json()
        assert delta['information_gain'] == 'NEW_METADATA_ONLY'
        assert all('metrics' not in e['structured_data'] for e in delta['new_evidence'])


def test_existing_state_and_package_tampering_are_refused(tmp_path, monkeypatch):
    folder = tmp_path / 'unrelated'; folder.mkdir(); sentinel = folder / 'account.txt'; sentinel.write_text('untouched')
    with pytest.raises(ValueError, match='ROOT_NOT_OWNED'):
        journey.build_app(folder)
    assert sentinel.read_text() == 'untouched'
    assert sorted(p.name for p in folder.iterdir()) == ['account.txt']
    asset = tmp_path / 'asset.json'; asset.write_text('{}')
    monkeypatch.setattr(journey, 'ASSET', asset)
    with pytest.raises(ValueError, match='DATASET_INTEGRITY'):
        journey.load_dataset()


def test_clean_process_import_ignores_host_environment(tmp_path):
    import os
    forbidden = tmp_path / 'DO_NOT_OPEN.sqlite3'
    script = ('from market_agent.portfolio_priority_journey import build_app; '
              f'build_app({str(tmp_path / "fresh")!r}); print("ISOLATED_BOOT_OK")')
    env = {**os.environ, 'MIA_DB_PATH': str(forbidden), 'MIA_PROVIDER_HUB': '1',
        'MIA_LONGBRIDGE_CREDENTIALS': str(tmp_path / 'NO_VAULT'), 'OPENAI_API_KEY': 'TEST_DO_NOT_USE',
        'MIA_RUNTIME_MODE': 'UNKNOWN_HOST_MODE'}
    result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'ISOLATED_BOOT_OK' in result.stdout
    assert not forbidden.exists()
    assert 'TEST_DO_NOT_USE' not in result.stdout + result.stderr


def test_failure_is_saved_read_only_recovery_then_explicit_retry(tmp_path, monkeypatch):
    from market_agent.llm import LLMError
    app = journey.build_app(tmp_path / 'fault')
    oracle = app.state.research_service.agent.llm
    original = oracle.complete_structured
    attempts = []
    async def failing(request):
        attempts.append(request.purpose)
        if request.purpose == 'research.assess':
            raise LLMError('transport', 'Scripted fault, no network')
        return await original(request)
    monkeypatch.setattr(oracle, 'complete_structured', failing)
    with TestClient(app, base_url=BASE, client=('127.0.0.1', 45004)) as c:
        failed = research(c)
        assert failed['status'] == 'FAILED'
        tid = failed['task_id']
        frozen = c.get(f'/api/research/tasks/{tid}/revisions/1').content
        before = len(attempts)
        for _ in range(3):
            assert c.get('/api/research/tasks/' + tid).json()['status'] == 'FAILED'
        assert len(attempts) == before
        monkeypatch.setattr(oracle, 'complete_structured', original)
        recovered = research(c, task_id=tid, mode='FAST', expected_revision=1)
        assert recovered['status'] == 'COMPLETE' and recovered['revision'] == 2
        assert c.get(f'/api/research/tasks/{tid}/revisions/1').content == frozen
        delta = c.get(f'/api/portfolio-priority/tasks/{tid}/delta').json()
        assert delta['information_gain'] == 'NO_NEW_EVIDENCE'
