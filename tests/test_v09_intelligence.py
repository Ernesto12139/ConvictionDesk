"""Production orchestration with fictional source/model oracles, never live calls."""
import asyncio
from decimal import Decimal
import time

from fastapi.testclient import TestClient
import pytest

from evals.v09_intelligence import create_app
from market_agent.research_task import ResearchRequest
from market_agent.valuation import valuation_report


def application(tmp_path,monkeypatch,*,no_gain=False):
    monkeypatch.setenv('MIA_ISOLATED_ROOT',str(tmp_path))
    monkeypatch.setenv('MIA_DB_PATH',str(tmp_path/'module-import.sqlite3'))
    monkeypatch.setenv('MIA_FIXTURE_NO_GAIN','1' if no_gain else '0')
    return create_app()


def run(app,symbol,mode):
    service=app.state.research_service
    task,previous,snapshot=service.begin(ResearchRequest(question=symbol+' 的经营变化影响投资逻辑吗？',mode=mode))
    asyncio.run(service.execute(task,previous,snapshot))
    task=service.tasks.get(task.task_id)
    assert task.status=='COMPLETE',task.error
    return task


@pytest.mark.parametrize('symbol',['APP','SMR'])
def test_deep_cites_actual_python_financials_and_company_specific_unknowns(tmp_path,monkeypatch,symbol):
    app=application(tmp_path,monkeypatch)
    fast,deep=run(app,symbol,'FAST'),run(app,symbol,'DEEP')
    assert fast.cutoff==deep.cutoff
    assert not any(c.name=='sec.fundamentals' for c in fast.tool_calls)
    receipt=next(s.evidence for s in deep.sources if s.evidence.structured_data.get('calculation')=='task_valuation_v1')
    assert receipt.evidence_id in deep.assessment.key_reasons[0].evidence_ids
    assert receipt.evidence_id in {row['evidence_id'] for row in deep.context_manifest['selected']}
    assert any(c.name=='valuation.project' and receipt.evidence_id in c.output_refs for c in deep.tool_calls)
    report=valuation_report(deep)
    if symbol=='APP':
        assert report.metrics['ttm_revenue'].value==Decimal('500')
        assert report.metrics['ttm_operating_cash_flow'].value==Decimal('92')
        assert report.metrics['pe_ttm'].value==Decimal('10')
        assert fast.action_decision.decision_action==deep.action_decision.decision_action=='ADD'
        assert deep.action_decision.position_status=='HELD'
    else:
        assert report.metrics['ttm_revenue'].value==0
        assert report.metrics['pe_ttm'].status==report.metrics['price_sales_ttm'].status=='NOT_MEANINGFUL'
        assert deep.action_decision.decision_action is None
        assert deep.action_decision.decision_status=='UNDETERMINED'
        assert deep.action_decision.position_status=='NOT_HELD'
    assert report.metrics['enterprise_value'].value is None
    assert deep.investigation['status']=='NEW_EVIDENCE'
    assert set(deep.investigation['new_evidence_ids']) <= set(deep.assessment.investigation_effect.evidence_ids)
    assert deep.assessment.judgment_change=='UNCHANGED'


def test_repeated_supplement_does_not_fabricate_information_gain(tmp_path,monkeypatch):
    app=application(tmp_path,monkeypatch,no_gain=True)
    task=run(app,'APP','DEEP')
    assert task.investigation['status']=='NO_NEW_EVIDENCE'
    assert task.investigation['new_evidence_ids']==[]
    assert task.assessment.judgment_change=='UNRESOLVED'
    assert '没有取得新增证据' in task.assessment.investigation_effect.text
    assert len([c for c in task.tool_calls if 'supplemental' in c.name])==1


def wait(client,path):
    for _ in range(250):
        response=client.get(path);assert response.status_code==200,response.text
        if response.json()['status']!='RUNNING':return response.json()
        time.sleep(.01)
    pytest.fail('Synthetic job did not terminate')


def test_manual_current_event_reaches_real_research_context_and_valuation(tmp_path,monkeypatch):
    app=application(tmp_path,monkeypatch)
    with TestClient(app) as client:
        assert not app.state.fixture_model.requests
        response=client.post('/api/attention/scan');assert response.status_code==202
        scan=wait(client,'/api/attention/scans/'+response.json()['scan_id'])
        assert scan['report']['screened']==2
        queue=client.get('/api/attention/queue').json()
        selected=next(row['item'] for row in queue['items'] if row['item']['symbol']=='APP')
        event_id=selected['packet']['evidence'][0]['evidence_id']
        response=client.post('/api/research/tasks',json={'question':'APP 的变化影响投资逻辑吗？',
            'mode':'DEEP','attention_item_id':selected['item_id']})
        assert response.status_code==202,response.text
        task=wait(client,'/api/research/tasks/'+response.json()['task_id'])
        assert task['status']=='COMPLETE',task['error']
        assert event_id in {s['evidence']['evidence_id'] for s in task['sources']}
        report=client.get('/api/research/tasks/'+task['task_id']+'/valuation?revision=1').json()
        assert Decimal(report['metrics']['pe_ttm']['value'])==10
        assert task['action_decision']['decision_action']=='ADD'
        assert queue['screen_batch_limit']==4


def test_actual_8f_sqlite_payload_roundtrip_prior_lookup_and_next_job(tmp_path,monkeypatch):
    import json
    from pathlib import Path
    from market_agent.tdb import content_hash
    app=application(tmp_path,monkeypatch)
    frozen=json.loads(Path('evals/fixtures/research_agent/v09-8f-valuation-task.json').read_text(encoding='utf-8'))
    raw,fingerprint=frozen['payload'],frozen['payload_hash'];payload=json.loads(raw)
    assert content_hash(payload)==fingerprint
    store=app.state.research_service.store
    with store._connect() as db:
        db.execute('INSERT INTO research_tasks VALUES(?,?,?,?,?,?)',
            (payload['task_id'],1,'COMPLETE',payload['updated_at'],fingerprint,raw))
        db.execute('INSERT INTO research_task_turns VALUES(?,?,?,?)',(payload['task_id'],1,fingerprint,raw))
    with TestClient(app) as client:
        path='/api/research/tasks/'+payload['task_id']
        assert content_hash(client.get(path).json())==fingerprint
        assert content_hash(client.get(path+'/revisions/1').json())==fingerprint
        for _ in range(2):
            report=client.get(path+'/valuation?revision=1')
            assert report.status_code==200
            assert Decimal(report.json()['metrics']['ttm_revenue']['value'])==1000
            assert report.json()['metrics']['market_cap_reference']['status']=='UNKNOWN'
        with store._connect() as db:
            assert db.execute('SELECT payload_hash,payload FROM research_tasks').fetchone()==(fingerprint,raw)
            assert db.execute('SELECT COUNT(*) FROM query_runs').fetchone()[0]==0
        response=client.post('/api/research/tasks',json={'task_id':payload['task_id'],
            'question':'APP 的经营变化影响投资逻辑吗？'})
        assert response.status_code==202,response.text
        completed=wait(client,path)
        assert completed['status']=='COMPLETE',completed['error']
        assert completed['revision']==2
        assert content_hash(client.get(path+'/revisions/1').json())==fingerprint
    with store._connect() as db:
        assert db.execute('SELECT payload_hash,payload FROM research_task_turns WHERE revision=1').fetchone()==(fingerprint,raw)
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
