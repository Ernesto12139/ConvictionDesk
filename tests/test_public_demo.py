"""Only the new public boundary; all records/providers are packaged synthetic."""
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from market_agent.main import app_from_environment, create_app
from market_agent.public_demo import create_public_demo, load_dataset


def make_app(tmp_path, **environment):
    return create_public_demo(create_app, {'MIA_DEMO_DIR': str(tmp_path / 'public'), **environment})


def rows(path):
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True) as db:
        tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {name:sorted(map(repr,db.execute('SELECT * FROM "'+name.replace('"','""')+'"'))) for name in tables}


def test_v08_today_to_fast_and_deep_action_journey_is_a_readonly_replay(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.get('/health').json() == {'status':'ok'}
        page = client.get('/').text
        assert 'data-runtime-mode="PUBLIC_DEMO"' in page and 'ConvictionDesk' in page
        manifest=client.get('/api/public-demo').json()
        assert manifest['execution_mode']=='OFFLINE_REPLAY' and manifest['synthetic'] is True
        assert manifest['access']=='SHARED_READ_ONLY' and manifest['model_budget_usd']=='0'
        assert manifest['scan_outcome']=='COMPLETED_WITH_EVENTS'
        # Today: real queue projection over the replayed scan (NY publication date).
        queue=client.get('/api/attention/queue').json()
        assert queue['scan_result_status']=='COMPLETED_WITH_EVENTS'
        assert queue['freshness']['market_timezone']=='America/New_York'
        assert queue['latest_scan']['report']['duplicates']==1
        items={row['item']['symbol']:row for row in queue['items']}
        assert set(items)=={'XMPL','XGRD'} and all(r['freshness']['freshness_status']=='TODAY' for r in items.values())
        assert items['XMPL']['item']['screen']['materiality']=='HIGH'
        assert client.get('/api/research-universe').status_code==200
        tasks={(e['symbol'],e['mode']):client.get(f"/api/research/tasks/{e['task_id']}/revisions/{e['revision']}").json()
               for e in manifest['examples']}
        fast,deep,other=tasks[('XMPL','FAST')],tasks[('XMPL','DEEP')],tasks[('XGRD','FAST')]
        for example in manifest['examples']:
            task=tasks[(example['symbol'],example['mode'])]
            assert task['attention_item_id']==example['attention_item_id']==items[example['symbol']]['item']['item_id']
            assert client.get('/api/attention/items/'+example['attention_item_id']).status_code==200
            ids={s['evidence']['evidence_id'] for s in task['sources']}
            assert set(task['action_decision']['source_ids'])<=ids
            assert all(e['model'].startswith('scripted-oracle') for e in task['model_events'])
        # Fast and Deep: same item, different context budget, tools and research depth.
        assert fast['mode']=='FAST' and deep.get('mode','DEEP')=='DEEP'
        assert fast['context_manifest']['max_context_bytes'] < deep['context_manifest']['max_context_bytes']
        fast_tools={c['name'] for c in fast['tool_calls']};deep_tools={c['name'] for c in deep['tool_calls']}
        assert 'sec.fundamentals' not in fast_tools and {'sec.fundamentals','sec.search.supplemental'}<=deep_tools
        assert fast['investigation']['status']=='NOT_NEEDED' and deep['investigation']['status']=='NEW_EVIDENCE'
        new=set(deep['investigation']['new_evidence_ids'])
        assert new and new<=set(deep['assessment']['investigation_effect']['evidence_ids'])
        assert deep['assessment']['judgment_change']=='UNCHANGED'
        quarters=[s for s in deep['sources'] if s['evidence']['structured_data'].get('calculation')=='sec_quarterly_v1']
        assert len(quarters)==4 and not any(s['evidence']['structured_data'].get('calculation')=='sec_quarterly_v1' for s in fast['sources'])
        derived={m['metric']:m for q in quarters for m in q['evidence']['structured_data']['quarter']['metrics']
                 if q['evidence']['structured_data']['quarter']['end']=='2025-12-31'}
        assert derived['revenue']['value']=='451000000' and derived['revenue']['basis']=='YTD_DIFFERENCE'
        assert 'diluted_eps' not in derived  # EPS is never derived by subtraction
        # Action: held issuer gets a provisional direction; unheld issuer stays undetermined, not HOLD.
        assert fast['action_decision']['decision_action']=='HOLD' and fast['action_decision']['position_status']=='HELD'
        assert fast['action_decision']['decision_status']=='PROVISIONAL' and fast['action_decision']['evidence_strength']=='LIMITED'
        assert deep['action_decision']['evidence_strength']=='MIXED'
        assert other['action_decision']['decision_action'] is None
        assert other['action_decision']['decision_status']=='UNDETERMINED' and other['action_decision']['position_status']=='NOT_HELD'
        assert client.get('/api/console/portfolio-overview').status_code==200
    restarted=make_app(tmp_path)
    with TestClient(restarted) as client:
        assert client.get(f"/api/research/tasks/{deep['task_id']}").json()==deep


def test_packaged_replay_is_reproducible_from_the_real_services(tmp_path, monkeypatch):
    import asyncio, importlib.util
    spec=importlib.util.spec_from_file_location('build_demo',Path(__file__).resolve().parents[1]/'scripts'/'build_public_demo_dataset.py')
    builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
    monkeypatch.setattr(builder,'DESTINATION',tmp_path/'replay.json')
    asyncio.run(builder.main())
    rebuilt=json.loads((tmp_path/'replay.json').read_text(encoding='utf-8'))['data']
    packaged=load_dataset()['data']
    assert rebuilt['model_requests']=={'scripted_oracle':8,'paid_model_requests':0}
    assert rebuilt['scan_outcome']==packaged['scan_outcome'] and sorted(rebuilt['tables'])==sorted(packaged['tables'])
    for name in ('attention_items','research_tasks'):
        assert len(rebuilt['tables'][name]['rows'])==len(packaged['tables'][name]['rows'])


def test_two_guests_can_only_read_the_intentionally_shared_synthetic_replay(tmp_path):
    app=make_app(tmp_path);path=tmp_path/'public/public-demo.sqlite3'
    with TestClient(app) as a,TestClient(app) as b:
        shared=a.get('/api/research/tasks').json()
        assert shared==b.get('/api/research/tasks').json()
        original=rows(path)
        for client in (a,b):
            for method,url,payload in [('post','/api/research/tasks',{'question':'APP live research','task_id':shared[0]['task_id']}),
                ('post','/api/attention/scan',{}),('put','/api/research-universe',{'symbol':'APP','state':'ARCHIVED'}),
                ('post','/api/portfolio',{}),('delete','/api/research/tasks/'+shared[0]['task_id'],{}),
                ('patch','/api/portfolio',{}),('post','/api/trade',{})]:
                response=client.request(method,url,json=payload)
                assert response.status_code==403 and response.json()['detail']=='PUBLIC_DEMO_READ_ONLY'
            assert client.get('/api/research/tasks/not-another-guests-task').status_code==404
            for route in ('/docs','/openapi.json','/qa/runtime/metrics','/api/jobs','/api/runs/private','/static/index.html'):
                assert client.get(route).status_code==404
        assert rows(path)==original


def test_poisoned_owner_environment_never_loads_credentials_models_or_owner_db(tmp_path,monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError('Public replay attempted external construction or I/O')
    import httpx
    import market_agent.local_credentials as credentials
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport,'handle_async_request',forbidden)
    monkeypatch.setattr(credentials,'load_credentials',forbidden)
    owner=tmp_path/'owner.sqlite3';owner.write_bytes(b'private-owner-sentinel')
    environment={'MIA_RUNTIME_MODE':'PUBLIC_DEMO','MIA_DEMO_DIR':str(tmp_path/'public'),
        'MIA_DB_PATH':str(owner),'MASSIVE_API_KEY':'not-a-real-key','FINNHUB_API_KEY':'not-a-real-key',
        'LLM_API_KEY':'not-a-real-key','JEV_API_KEY':'not-a-real-key','MIA_RESEARCH_MCP':'readonly',
        'MIA_LONGBRIDGE_CREDENTIALS':str(tmp_path/'never-read.dpapi'),
        'MIA_CAPITAL_REFERENCE_MODE':'personal-display','MIA_CAPITAL_REFERENCE_DIR':str(tmp_path/'never-read-capital'),
        'MIA_SEC_RETRIEVAL':'hybrid','MIA_EMBEDDING_MODEL_DIR':str(tmp_path/'never-read-model')}
    for key,value in environment.items():monkeypatch.setenv(key,value)
    with TestClient(app_from_environment(environment)) as client:
        assert client.get('/api/console/portfolio-overview').status_code==200
        status=client.get('/api/console/status').json()
        assert status['synthesis_mode']=='unconfigured' and status['comparison_mode']=='off'
        assert client.post('/api/research/tasks',json={'question':'APP'}).status_code==403
        assert 'not-a-real-key' not in json.dumps(status)
        example=client.get('/api/public-demo').json()['examples'][0]
        report=client.get('/api/research/tasks/'+example['task_id']+'/valuation').json()
        assert 'local_capital_reference' not in report
    assert owner.read_bytes()==b'private-owner-sentinel'


@pytest.mark.parametrize('mode',['INVITED_LIVE_RESEARCH','live','UNKNOWN'])
def test_live_execution_configuration_fails_closed(tmp_path,mode):
    with pytest.raises(ValueError,match='disabled'):
        make_app(tmp_path,MIA_RESEARCH_EXECUTION=mode)
    assert not (tmp_path/'public').exists()


def test_unknown_runtime_and_non_demo_directory_are_rejected_without_touching_data(tmp_path):
    with pytest.raises(ValueError,match='MIA_RUNTIME_MODE'):
        app_from_environment({'MIA_RUNTIME_MODE':'PUBLC_DEMO'})
    folder=tmp_path/'public';folder.mkdir();sentinel=folder/'owner.db';sentinel.write_bytes(b'private')
    with pytest.raises(ValueError,match='Refusing'):
        make_app(tmp_path)
    assert sentinel.read_bytes()==b'private' and list(folder.iterdir())==[sentinel]


def test_tampered_history_is_not_reset_or_accepted_on_restart(tmp_path):
    make_app(tmp_path);path=tmp_path/'public/public-demo.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute("UPDATE research_task_turns SET payload_hash='tampered'")
    with pytest.raises(Exception,match='RESEARCH_TASK_INTEGRITY'):
        make_app(tmp_path)


def test_contaminated_demo_volume_is_rejected_before_schema_or_data_changes(tmp_path):
    make_app(tmp_path);path=tmp_path/'public/public-demo.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO research_universe VALUES('PRIVATE','WATCHING','private','private','private','private',NULL)")
    original=path.read_bytes()
    with pytest.raises(ValueError,match='outside the packaged replay'):
        make_app(tmp_path)
    assert path.read_bytes()==original


def test_marker_link_is_rejected_before_reading_its_contents(tmp_path,monkeypatch):
    import os
    folder=tmp_path/'public';folder.mkdir()
    private=tmp_path/'not-a-real-secret.json';private.write_text('{"access_token":"synthetic-secret"}')
    os.link(private,folder/'public-demo.json')
    original=Path.read_text
    def read(path,*args,**kwargs):
        if path.name=='public-demo.json':
            raise AssertionError('Linked marker must not be read')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read)
    with pytest.raises(ValueError,match='marker cannot be a link'):
        make_app(tmp_path)


def test_demo_generation_cannot_bypass_evidence_and_numeric_validators(tmp_path, monkeypatch):
    import asyncio, importlib.util
    spec=importlib.util.spec_from_file_location('build_demo',Path(__file__).resolve().parents[1]/'scripts'/'build_public_demo_dataset.py')
    builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
    monkeypatch.setattr(builder,'DESTINATION',tmp_path/'replay.json')
    original=builder.ScriptedOracle.assess
    def invented(self, request):
        data=original(self, request)
        data['core_judgment']={**data['core_judgment'],'text':'倾向HOLD：收入将增长35%。'}
        return data
    monkeypatch.setattr(builder.ScriptedOracle,'assess',invented)
    with pytest.raises(AssertionError,match='unsupported_numeric_fact'):
        asyncio.run(builder.main())
    assert not (tmp_path/'replay.json').exists()
