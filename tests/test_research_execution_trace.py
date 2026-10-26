import json
import socket
import time

import pytest
from fastapi.testclient import TestClient

from market_agent.public_ivs import build_app
from market_agent.research_execution_trace import ResearchExecutionTrace, install_execution_trace
from market_agent.run_trace import CodeIdentity

BASE = 'http://127.0.0.1:8896'


@pytest.fixture
def observed(tmp_path, monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {'127.0.0.1', 'localhost', '::1'}:
            raise AssertionError('Unexpected network request')
        return original(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    app = build_app(tmp_path / 'synthetic')
    documents = []
    # This is a test identity, not release evidence. Recorded real replays get
    # their separately computed exact Git/tree/build identity after freezing.
    observer = ResearchExecutionTrace(code=CodeIdentity(commit='test-unfrozen', dirty=True),
                                      sink=documents.append, execution_mode='KEYLESS_SCRIPTED')
    install_execution_trace(app, observer)
    with TestClient(app, base_url=BASE, client=('127.0.0.1', 1234)) as client:
        yield client, app, documents


def finish(client, documents, body):
    response = client.post('/api/research/tasks', json=body)
    assert response.status_code == 202, response.text
    task_id = response.json()['task_id']
    for _ in range(1000):
        task = client.get('/api/research/tasks/' + task_id).json()
        if task['status'] != 'RUNNING' and any(d['task_id'] == task_id and
                d['revision_id'] == str(task['revision']) for d in documents):
            return task
        time.sleep(.02)
    raise AssertionError('actual observed job did not finish')


def test_actual_product_tools_models_revisions_and_whatif(observed):
    client, app, documents = observed
    fast = finish(client, documents, {'question': 'UNP 的 SEC 披露需要核查什么？', 'mode': 'FAST'})
    assert fast['status'] == 'COMPLETE'
    task_id = fast['task_id']
    first = client.get(f'/api/research/tasks/{task_id}/revisions/1').content
    trace = documents[-1]
    tools = [span for span in trace['spans'] if span['kind'] == 'tool']
    assert [span['name'] for span in tools] == [call['name'] for call in fast['tool_calls']]
    assert len(tools) > 0
    assert all(span['started_at'] <= span['ended_at'] and span['latency_ms'] >= 0 for span in tools)
    model_spans = [span for span in trace['spans'] if span['kind'] == 'model']
    assert len(model_spans) == len(fast['model_events']) > 0
    assert all(span['model']['execution'] == 'SCRIPTED' and
               span['model']['cost']['status'] == 'NOT_APPLICABLE' for span in model_spans)
    assert any(span['sources'] for span in trace['spans'])
    assert trace['pit_cutoff'] == fast['cutoff']
    deep = finish(client, documents, {'question': 'UNP 的披露还需要什么反证？', 'mode': 'DEEP', 'task_id': task_id})
    assert deep['status'] == 'COMPLETE' and deep['revision'] == 2
    assert client.get(f'/api/research/tasks/{task_id}/revisions/1').content == first
    body = {'revision': 2, 'account_id': 'PUBLIC-SYNTHETIC', 'symbol': 'UNP', 'side': 'BUY',
            'shares': '5', 'fill_price': '240', 'fee': '1', 'scenario_date': '2026-10-24',
            'other_prices': {'KR': '58'}, 'unchanged_share_basis_attested': True}
    result = client.post(f'/api/research/tasks/{task_id}/what-if', json=body)
    assert result.status_code == 200, result.text
    assert result.json()['after']['nav'] == '12979'
    whatif = documents[-1]
    assert whatif['run_id'].endswith(':whatif')
    assert whatif['spans'][0]['name'] == 'whatif.decimal'
    assert whatif['spans'][0]['notes'][0] == 'HYPOTHETICAL_RECEIPT_ONLY'
    assert client.get(f'/api/research/tasks/{task_id}/revisions/1').content == first
    # No question, quantity, account cash, raw payload or document body in receipts.
    raw = json.dumps(documents, ensure_ascii=False)
    assert 'SEC 披露需要核查什么' not in raw and 'fill_price' not in raw and 'avg_cost' not in raw


def test_real_validation_refusal_has_a_failed_execution_receipt(observed, monkeypatch):
    client, app, documents = observed
    oracle = app.state.research_service.model_profiles.client('public-fast')
    original = oracle.complete_structured
    async def invalid_citation(request):
        result = await original(request)
        if request.purpose in {'research.assess', 'research.reassess'}:
            result = result.model_copy(update={'core_judgment': result.core_judgment.model_copy(
                update={'evidence_ids': ['NONEXISTENT_SOURCE']})})
        return result
    monkeypatch.setattr(oracle, 'complete_structured', invalid_citation)
    task = finish(client, documents, {'question': 'UNP 的披露是否影响原逻辑？', 'mode': 'FAST'})
    assert task['status'] == 'FAILED' and task['error'] == 'invalid_evidence_reference'
    trace = documents[-1]
    assert trace['status'] == 'FAILED' and trace['error_code'] == 'invalid_evidence_reference'
    assert any(p['code'] == 'invalid_evidence_reference' for span in trace['spans'] for p in span['policies'])
    assert len([s for s in trace['spans'] if s['kind'] == 'tool']) == len(task['tool_calls'])
    assert task['assessment'] is None


def test_missing_price_preserves_unknown_in_actual_traced_whatif(observed):
    client, app, documents = observed
    task = finish(client, documents, {'question': 'UNP 的披露需要核查什么？', 'mode': 'FAST'})
    response = client.post(f"/api/research/tasks/{task['task_id']}/what-if", json={
        'revision': 1, 'account_id': 'PUBLIC-SYNTHETIC', 'symbol': 'UNP', 'side': 'BUY',
        'shares': '5', 'fill_price': '240', 'fee': '1', 'scenario_date': '2026-10-24',
        'other_prices': {}, 'unchanged_share_basis_attested': True})
    assert response.status_code == 200
    assert response.json()['after']['nav'] is None
    assert documents[-1]['status'] == 'COMPLETE'


def test_keyless_observer_cannot_label_a_native_adapter_as_scripted(observed):
    client, app, documents = observed
    from types import SimpleNamespace
    observer = app.state.research_service.execution_observer
    with pytest.raises(ValueError, match='TRACE_EXECUTION_MODE_MISMATCH'):
        with observer.model(None, SimpleNamespace(model_name='native-adapter'), 'research.assess', [], None):
            raise AssertionError('native dispatch was admitted')


def test_missing_focus_is_a_blocked_request_not_a_research_judgment(observed):
    client, app, documents = observed
    task = finish(client, documents, {'question': '有什么值得关注？', 'mode': 'FAST'})
    assert task['status'] == 'NEEDS_INPUT' and task['assessment'] is None
    assert documents[-1]['status'] == 'BLOCKED'
    assert documents[-1]['error_code'] == 'NEEDS_INPUT'
