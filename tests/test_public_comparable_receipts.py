"""Packaged PUBLIC_SEC only: typed receipts reach the default scripted case file."""
from copy import deepcopy
import re
import socket
import time

from fastapi.testclient import TestClient
import pytest

from market_agent import portfolio_priority_journey as journey
from market_agent.evidence_action import NumericReceipt, build_evidence_packet, contract_request
from market_agent.research_task import ResearchSource, ResearchTask
from market_agent.tdb import content_hash


PREVIOUS = {'start': '2025-01-01', 'end': '2025-06-30'}
CURRENT = {'start': '2026-01-01', 'end': '2026-06-30'}
EXPECTED = {
    'comparison.revenue.previous': ('2417728000', 'usd', PREVIOUS),
    'comparison.revenue.current': ('3766135000', 'usd', CURRENT),
    'comparison.revenue.change': ('1348407000', 'usd', {'previous': PREVIOUS, 'current': CURRENT}),
    'comparison.revenue.yoy_pct': ('55.77', 'percent', {'previous': PREVIOUS, 'current': CURRENT}),
    'comparison.operating_cash_flow.previous': ('1603938000', 'usd', PREVIOUS),
    'comparison.operating_cash_flow.current': ('2160433000', 'usd', CURRENT),
    'comparison.operating_cash_flow.change': ('556495000', 'usd', {'previous': PREVIOUS, 'current': CURRENT}),
    'comparison.operating_cash_flow.yoy_pct': ('34.70', 'percent', {'previous': PREVIOUS, 'current': CURRENT}),
}


@pytest.fixture(autouse=True)
def no_external(monkeypatch):
    original = socket.socket.connect
    def guarded(sock, address):
        if isinstance(address, tuple) and address[0] not in {'localhost', '127.0.0.1', '::1'}:
            raise AssertionError('EXTERNAL_NETWORK_FORBIDDEN')
        return original(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', guarded)


def source_packet(evidence=None):
    data = journey.load_dataset()
    cutoff = journey.instant(data['as_of'])
    evidence = evidence or journey.financial_packet(data, cutoff)
    source = ResearchSource(identity='VERIFIED_FACT', evidence=evidence, freshness='CURRENT')
    task = ResearchTask(task_id='public-receipt-synthetic-test', revision=1, status='COMPLETE',
        question='APP packaged public filing comparison, no live model.', objective='EARNINGS_CHECK',
        focus_symbol='APP', mode='DEEP', cutoff=cutoff, updated_at=cutoff, sources=[source])
    return task, build_evidence_packet(task, sources=[source])


def research(client, **changes):
    response = client.post('/api/portfolio-priority/research', json={
        'symbol': 'APP', 'account_id': 'SYN-GROWTH', **changes})
    assert response.status_code == 202, response.text
    task_id = response.json()['task_id']
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        task = client.get('/api/research/tasks/' + task_id).json()
        if task['status'] != 'RUNNING':
            return task
        time.sleep(.02)
    raise AssertionError('SCRIPTED_TASK_DEADLINE')


def test_existing_public_capture_has_eight_exact_typed_receipts_without_changing_source():
    task, packet = source_packet()
    original = task.model_dump_json()
    source = task.sources[0].evidence
    rows = {r.field: r for r in packet.receipts}
    assert set(rows) == set(EXPECTED)
    for field, (value, unit, period) in EXPECTED.items():
        row = rows[field]
        assert (row.value, row.unit, row.period) == (value, unit, period)
        assert row.evidence_id == source.evidence_id
        assert row.as_of == source.as_of
        assert 'accession=0001751008-26-000059' in row.basis
        assert 'scope=ENTIRE_FILING_ENTITY' in row.basis
        assert 'CONTINUING_OPERATIONS' not in row.basis
    request = contract_request(packet)
    assert len(request['facts']['numeric_receipts']) == 8
    assert len({r.receipt_id for r in rows.values()}) == 8
    assert task.model_dump_json() == original
    assert content_hash(source.structured_data) == source.content_hash


@pytest.mark.parametrize('fault', [
    'period', 'unit', 'accession', 'scope', 'concept', 'future_available',
    'missing', 'nonfinite', 'derived_value', 'hash', 'provider', 'source_uri',
])
def test_invalid_public_comparison_never_acquires_receipts(fault):
    task, _ = source_packet()
    original = task.sources[0].evidence
    data = deepcopy(original.structured_data)
    changes = {}
    if fault == 'period': data['metrics'][0]['start'] = '2025-04-01'
    elif fault == 'unit': data['metrics'][0]['unit'] = 'EUR'
    elif fault == 'accession': data['metrics'][0]['accession_number'] = '0001751008-26-000060'
    elif fault == 'scope': data['metrics'][0]['scope'] = 'CONTINUING_OPERATIONS'
    elif fault == 'concept': data['metrics'][0]['concept'] = 'OtherRevenue'
    elif fault == 'future_available': data['metrics'][0]['available_at'] = '2099-01-01T00:00:00+00:00'
    elif fault == 'missing': data['metrics'][0].pop('value')
    elif fault == 'nonfinite': data['metrics'][0]['value'] = 'NaN'
    elif fault == 'derived_value': data['comparison']['revenue']['change'] = '1'
    elif fault == 'provider': changes['provider'] = 'unverified'
    elif fault == 'source_uri': changes['source_uri'] = 'https://example.com/unqualified'
    changes.update(structured_data=data, content_hash='wrong-hash' if fault == 'hash' else content_hash(data))
    _, packet = source_packet(original.model_copy(update=changes))
    assert packet.receipts == []


def test_unselected_source_does_not_leak_comparison_receipts():
    task, _ = source_packet()
    assert build_evidence_packet(task, sources=[]).receipts == []


def test_default_keyless_fast_deep_delta_delivers_receipts_read_only(tmp_path):
    app = journey.build_app(tmp_path / 'synthetic')
    with TestClient(app, base_url='http://127.0.0.1:8793', client=('127.0.0.1', 45005)) as client:
        fast = research(client)
        tid = fast['task_id']
        route = f'/api/portfolio-priority/tasks/{tid}/delta'
        assert client.get(route).json()['numeric_receipts'] == []
        frozen = client.get(f'/api/research/tasks/{tid}/revisions/1').content
        deep = research(client, task_id=tid, expected_revision=1, mode='DEEP')
        assert deep['status'] == 'COMPLETE', deep.get('error')
        saved = client.get(f'/api/research/tasks/{tid}/revisions/2').content
        snapshot = client.get('/api/portfolio/current').content
        delta = client.get(route).json()
        rows = [NumericReceipt.model_validate(row) for row in delta['numeric_receipts']]
        assert len(rows) == 8 and {r.field for r in rows} == set(EXPECTED)
        assert re.fullmatch('[0-9a-f]{64}', delta['numeric_receipt_packet_hash'])
        source = next(e for e in delta['new_evidence'] if e['structured_data'].get('metrics'))
        assert {r.evidence_id for r in rows} == {source['evidence_id']}
        assert delta['numeric_receipt_sources'] == [{
            'evidence_id': source['evidence_id'], 'source_hash': source['content_hash'],
            'source_uri': source['source_uri'], 'ref': rows[0].evidence_ref}]
        assert client.get(f'/api/research/tasks/{tid}/revisions/1').content == frozen
        assert client.get(f'/api/research/tasks/{tid}/revisions/2').content == saved
        assert client.get('/api/portfolio/current').content == snapshot
        assert delta['model'] == 'SCRIPTED_NOT_LIVE'
        assert all(e['provider'] == 'scripted-local' for e in deep['model_events'])
        assert client.get(route + '?revision=1').json()['numeric_receipts'] == []
