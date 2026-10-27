import json
import socket
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import pytest

from market_agent.unified_workbench import COOKIE, build_app, checked_root

BASE = 'http://127.0.0.1:8792'


@pytest.fixture
def client(tmp_path, monkeypatch):
    original = socket.socket.connect
    def guarded(self, address):
        if isinstance(address, tuple) and address[0] not in {'127.0.0.1','::1','localhost'}:
            raise AssertionError('Unexpected external provider connection')
        return original(self, address)
    monkeypatch.setattr(socket.socket, 'connect', guarded)
    with TestClient(build_app(tmp_path / 'scopes'), base_url=BASE, client=('127.0.0.1',51234)) as client:
        yield client


def switch(client, scope, origin=BASE):
    return client.post('/api/workbench/mode', json={'scope':scope},
                       headers={'Origin':origin,'X-Workbench-Mode':'select'})


def test_explicit_scope_no_cross_database_adoption(client):
    assert client.get('/api/workbench/mode').json()['scope'] == 'SYNTHETIC'
    assert client.get('/api/workbench/mode').json()['owner_data_integration'] == 'NOT_CONNECTED'
    demo = client.get('/api/console/portfolio-overview')
    assert demo.status_code == 200
    assert switch(client, 'OWNER_PREVIEW').status_code == 200
    mode = client.get('/api/workbench/mode').json()
    assert mode['data_kind'] == 'EMPTY_ISOLATED_SANDBOX'
    assert mode['paid_dispatch'] == 'BLOCKED' and mode['real_owner_database_opened'] is False
    assert client.get('/api/console/portfolio-overview').status_code == 404
    assert not any(client.get('/api/research/model-profiles').json()['mode_availability'].values())
    assert client.post('/api/research/tasks', json={'question':'UNP','mode':'FAST'}).json()['detail'] == 'OWNER_SCOPE_NOT_ADMITTED'
    assert client.post('/api/portfolio', json={}).status_code == 409
    assert switch(client, 'SYNTHETIC').status_code == 200
    assert client.get('/api/console/portfolio-overview').json() == demo.json()


def test_mode_origin_payload_and_forged_cookie_fail_closed(client):
    assert switch(client,'OWNER_PREVIEW',origin='https://foreign.invalid').status_code == 403
    assert client.get('/api/workbench/mode').json()['scope'] == 'SYNTHETIC'
    assert switch(client,'REAL_OWNER').status_code == 422
    client.cookies.set(COOKIE,'REAL_OWNER')
    assert client.get('/api/research/tasks').status_code == 400
    assert client.get('/api/workbench/mode').status_code == 400


def test_foreign_host_and_peer_refused(tmp_path):
    app = build_app(tmp_path / 'scopes')
    with TestClient(app,base_url='http://foreign.invalid') as client:
        assert client.get('/').status_code == 403
    with TestClient(app,base_url=BASE,client=('192.0.2.1',1234)) as client:
        assert client.get('/').status_code == 403


def test_original_research_revision_preserved_across_modes(client):
    scan = client.post('/api/today/filings/refresh',json={}).json()
    filing = next(row for row in scan['filings'] if row['symbol'] == 'UNP')
    response = client.post('/api/research/tasks',json={'question':'UNP 的 SEC 披露应核查什么？','mode':'FAST','filing_evidence_id':filing['evidence_id']})
    assert response.status_code == 202, response.text
    task_id = response.json()['task_id']
    for _ in range(200):
        task = client.get('/api/research/tasks/'+task_id).json()
        if task['status'] != 'RUNNING':
            break
        time.sleep(.02)
    assert task['status'] == 'COMPLETE', task.get('error')
    receipts = client.get(f'/api/research/tasks/{task_id}/execution-traces').json()
    assert receipts['execution_mode'] == 'KEYLESS_SCRIPTED'
    assert receipts['receipts']
    receipt_id = receipts['receipts'][0]['trace_id']
    assert client.get('/api/execution-traces/' + receipt_id).status_code == 200
    original = client.get(f'/api/research/tasks/{task_id}/revisions/1').content
    assert switch(client,'OWNER_PREVIEW').status_code == 200
    assert client.get('/api/execution-traces/' + receipt_id).status_code == 404
    assert client.get('/api/research/tasks/'+task_id).status_code == 404
    assert switch(client,'SYNTHETIC').status_code == 200
    assert client.get(f'/api/research/tasks/{task_id}/revisions/1').content == original


def test_existing_unmarked_root_and_database_refused(tmp_path):
    root = tmp_path / 'foreign'
    root.mkdir()
    (root / 'owner.sqlite3').write_bytes(b'UNRELATED DO NOT TOUCH')
    with pytest.raises(ValueError,match='UNMARKED_ROOT'):
        build_app(root)
    assert (root / 'owner.sqlite3').read_bytes() == b'UNRELATED DO NOT TOUCH'
    root = checked_root(tmp_path / 'marked')
    owner = root / 'owner-preview'
    owner.mkdir()
    (owner / 'preview.sqlite3').write_bytes(b'UNRELATED DO NOT TOUCH')
    with pytest.raises(ValueError,match='UNMARKED_DATABASE'):
        build_app(root)
    assert (owner / 'preview.sqlite3').read_bytes() == b'UNRELATED DO NOT TOUCH'


def test_nested_symlink_refused_before_store_access(tmp_path):
    root = checked_root(tmp_path / 'marked')
    outside = tmp_path / 'outside'
    outside.mkdir()
    try:
        (root / 'synthetic').symlink_to(outside,target_is_directory=True)
    except OSError:
        pytest.skip('Windows symlink privilege unavailable; junction negative covered separately')
    with pytest.raises(ValueError,match='LINK_REFUSED'):
        build_app(root)
    assert not list(outside.iterdir())


def test_cli_refuses_owner_port_before_constructing_state(tmp_path):
    root = tmp_path / 'never-created'
    result = subprocess.run([sys.executable,'-m','market_agent.unified_workbench','--root',str(root),'--port','8000'],capture_output=True,text=True)
    assert result.returncode != 0 and 'OWNER_PORT_8000_REFUSED' in result.stderr
    assert not root.exists()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows junction-specific negative')
def test_windows_junction_refused(tmp_path):
    import _winapi
    root = checked_root(tmp_path / 'marked')
    outside = tmp_path / 'outside'
    outside.mkdir()
    _winapi.CreateJunction(str(outside), str(root / 'synthetic'))
    with pytest.raises(ValueError, match='LINK_REFUSED'):
        build_app(root)
    assert not list(outside.iterdir())
