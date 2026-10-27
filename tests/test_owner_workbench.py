import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from market_agent.owner_workbench import OwnerInputs, build_owner_workbench
from market_agent.owner_value_slice import SecFilingBody
from market_agent.persistence import Store
from market_agent.provider_vault import ProviderVault
from market_agent.tdb import content_hash
from market_agent.unified_workbench import COOKIE
from test_owner_value_slice import snapshot, event, thesis, load, EVAL

BASE = 'http://127.0.0.1:8797'


@pytest.fixture
def built(tmp_path, monkeypatch):
    monkeypatch.setattr(ProviderVault, 'bridge', lambda *args, **kwargs: None)
    database = tmp_path / 'owner.sqlite3'
    store = Store(database)
    snap = snapshot()
    store.add_snapshot(snap)
    inputs = OwnerInputs(snapshot_id=snap.snapshot_id,
        snapshot_hash=content_hash(snap.model_dump(mode='json')),
        account_id=snap.accounts[0].account_id, symbol='GLXY', event=event(),
        filing_body=SecFilingBody.model_validate(load('sec_body_8k_5_02.json')), thesis=thesis())
    root = tmp_path / 'runtime'
    app = build_owner_workbench(root, database, inputs,
        {'MIA_PROVIDER_HUB':'1', 'MIA_RUNTIME_MODE':'OWNER'}, clock=lambda:EVAL)
    return app, store, root


def test_local_owner_qualification_whatif_trace_and_scope_isolation(built):
    app, store, root = built
    original = store.get_snapshot().model_dump(mode='json')
    with TestClient(app, base_url=BASE, client=('127.0.0.1',1)) as client:
        assert client.get('/api/workbench/mode').json()['scope'] == 'OWNER_LOCAL'
        result = client.get('/api/owner/value')
        assert result.status_code == 200, result.text
        data = result.json()
        assert data['qualification']['body'] == 'BODY_VERIFIED'
        assert data['qualification']['price'] == 'UNAVAILABLE'
        assert data['view']['valuation']['status'] == 'UNKNOWN'
        receipt_id = data['trace_id']
        trace = client.get('/api/execution-traces/' + receipt_id).json()
        assert trace['execution_mode'] == 'OWNER_LOCAL'
        assert [s['name'] for s in trace['spans'] if s['kind']=='tool'] == [
            'portfolio.local_snapshot', 'owner_value.qualify_and_compute']
        serialized = json.dumps(trace)
        assert json.dumps(original['accounts'][0]['account_id']) not in serialized
        assert all('account_id' not in s.get('input_class', {}) for s in trace['spans'])
        csrf = client.get('/api/providers/session').json()['csrf_token']
        body = {'side':'BUY','shares':'1','price':'10','price_basis':'HYPOTHETICAL_OPERATOR_INPUT','rationale':'test explicit scenario'}
        assert client.post('/api/owner/whatif', json=body).status_code == 403
        result = client.post('/api/owner/whatif',json=body,headers={'Origin':BASE,'X-Provider-CSRF':csrf})
        assert result.status_code == 200, result.text
        assert result.json()['view']['whatif']['nav_basis'] == 'UNKNOWN'
        assert store.get_snapshot().model_dump(mode='json') == original
        switched = client.post('/api/workbench/mode',json={'scope':'SYNTHETIC'},
            headers={'Origin':BASE,'X-Workbench-Mode':'select'})
        assert switched.status_code == 200
        assert client.get('/api/owner/value').status_code == 404
        assert client.get('/api/execution-traces/' + receipt_id).status_code == 404
        assert 'owner-value-status' not in client.get('/').text
    assert not (root / 'UNIFIED_PREVIEW_ONLY.json').exists()
    assert (root / 'sample/UNIFIED_PREVIEW_ONLY.json').exists()


def test_snapshot_change_and_cross_origin_fail_closed(built, monkeypatch):
    app, store, root = built
    with TestClient(app,base_url=BASE,client=('127.0.0.1',1)) as client:
        assert client.get('/api/owner/value',headers={'Origin':'https://foreign.invalid'}).status_code == 403
        assert client.get('/api/owner/value',headers={'X-Forwarded-For':'127.0.0.1'}).status_code == 403
        # No input bundle can be supplied over HTTP.
        assert client.post('/api/owner/value',json={}).status_code == 403
        app.state.owner_inputs  # Frozen server input, never an HTTP payload.
        from market_agent.unified_workbench import Scope
        local_store = app.state.scopes[Scope.OWNER_LOCAL].state.research_service.store
        monkeypatch.setattr(local_store, 'get_snapshot', lambda: None)
        response = client.get('/api/owner/value')
        assert response.status_code == 409
        assert response.json()['detail']['code'] == 'OWNER_INPUT_SNAPSHOT_CHANGED'


def test_keyless_cannot_opt_in_to_owner_by_forged_cookie(tmp_path):
    from market_agent.unified_workbench import build_app
    with TestClient(build_app(tmp_path/'sample'),base_url=BASE,client=('127.0.0.1',1)) as client:
        client.cookies.set(COOKIE,'OWNER_LOCAL')
        assert client.get('/api/workbench/mode').status_code == 403
        assert client.get('/api/owner/value').status_code == 403


def test_unqualified_data_source_keys_do_not_enable_owner_clients(tmp_path, monkeypatch):
    monkeypatch.setattr(ProviderVault, 'bridge', lambda *args, **kwargs: None)
    database = tmp_path / 'owner.sqlite3'
    store = Store(database)
    snap = snapshot()
    store.add_snapshot(snap)
    inputs = OwnerInputs(snapshot_id=snap.snapshot_id,
        snapshot_hash=content_hash(snap.model_dump(mode='json')),
        account_id=snap.accounts[0].account_id, symbol='GLXY', event=event())
    env = {'MIA_PROVIDER_HUB':'1', 'MIA_RUNTIME_MODE':'OWNER',
           'FINNHUB_API_KEY':'finnhub-test', 'MASSIVE_API_KEY':'massive-test',
           'LONGBRIDGE_MCP_ACCESS_TOKEN':'longbridge-test'}
    app = build_owner_workbench(tmp_path/'runtime',database,inputs,env,clock=lambda:EVAL)
    with TestClient(app,base_url=BASE,client=('127.0.0.1',1)) as client:
        response = client.get('/api/provider-source-health')
        assert response.status_code == 200
        rows = {row['provider']:row for row in response.json()['providers']}
        for vendor in ('Finnhub','Massive','Longbridge'):
            assert 'RIGHTS_BLOCKED' in rows[vendor]['states']
            assert rows[vendor]['source_client_enabled'] is False
        assert all(value not in response.text for value in ('finnhub-test','massive-test','longbridge-test'))


def test_real_launcher_identity_reaches_scoped_app_and_refuses_unadmitted_8000(built, tmp_path):
    import json
    import os
    import shutil
    import socket
    import subprocess
    import sys
    import time
    import urllib.request
    app, store, root = built
    database = tmp_path / 'owner.sqlite3'
    backup = tmp_path / 'backup.sqlite3'
    shutil.copy2(database, backup)
    inputs = tmp_path / 'inputs.json'
    inputs.write_text(app.state.owner_inputs.model_dump_json(), encoding='utf-8')
    environment = {k:v for k,v in os.environ.items() if k.upper() in {
        'SYSTEMROOT','SYSTEMDRIVE','WINDIR','PATH','PATHEXT','COMSPEC','TEMP','TMP','OS'}}
    for name in ('LOCALAPPDATA','APPDATA','USERPROFILE'):
        directory=tmp_path/name
        directory.mkdir()
        environment[name]=str(directory)
    environment['PYTHONPATH']=str(Path(__file__).resolve().parents[1]/'src')
    environment['PYTHONUTF8']='1'
    # This HTTP/launcher test has an explicitly empty fixture Vault. Hosted CI
    # must not depend on, or attempt to read, the runner account's DPAPI store.
    bootstrap=('from market_agent.provider_vault import ProviderVault; '
        'ProviderVault.bridge=lambda self,operation,provider,secret=None: None; '
        'from market_agent.owner_workbench import main; main()')
    command=[sys.executable,'-c',bootstrap,'--root',str(tmp_path/'cli'),
        '--database',str(database),'--backup',str(backup),'--inputs',str(inputs)]
    refused=subprocess.run(command+['--port','8000'],env=environment,capture_output=True,text=True,timeout=30)
    assert refused.returncode != 0
    assert 'PRODUCTION_ADMISSION_REQUIRED' in refused.stderr
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0))
        port=probe.getsockname()[1]
    with (tmp_path/'cli.log').open('w') as log:
        process=subprocess.Popen(command+['--port',str(port)],env=environment,stdout=log,stderr=log)
        try:
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/workbench/identity',timeout=1) as response:
                        identity=json.load(response)
                    break
                except OSError:
                    assert process.poll() is None, (tmp_path/'cli.log').read_text(encoding='utf-8',errors='replace')
                    time.sleep(.1)
            else:
                pytest.fail('Identity route did not become available')
            assert identity['port']==port
            assert identity['startup_model_calls']==0
            assert identity['scope']=='OWNER_LOCAL_WITH_ISOLATED_KEYLESS_SAMPLE'
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/provider-source-health',timeout=2) as response:
                health=json.load(response)
            assert not any(row['configured'] for row in health['providers'])
        finally:
            process.terminate()
            process.wait(timeout=10)
