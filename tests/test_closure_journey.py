"""One coherent Today -> Fast -> same-task Deep -> scenario -> saved revisions."""
from decimal import Decimal
import time

from fastapi.testclient import TestClient
import pytest

from evals.closure_journey import create_app, NOW
from market_agent.tdb import content_hash


def application(tmp_path,monkeypatch,no_gain=False):
    monkeypatch.setenv('MIA_ISOLATED_ROOT',str(tmp_path))
    monkeypatch.setenv('MIA_DB_PATH',str(tmp_path/'import.sqlite3'))
    monkeypatch.setenv('MIA_FIXTURE_NO_GAIN','1' if no_gain else '0')
    return create_app()


def wait(client,path):
    for _ in range(300):
        response=client.get(path)
        assert response.status_code==200,response.text
        if response.json()['status']!='RUNNING':return response.json()
        time.sleep(.01)
    pytest.fail('Offline scripted task did not finish')


@pytest.mark.parametrize('no_gain',[False,True])
def test_same_event_thesis_fast_deep_and_price_only_delta(tmp_path,monkeypatch,no_gain):
    app=application(tmp_path,monkeypatch,no_gain)
    with TestClient(app) as client:
        scan=client.post('/api/attention/scan')
        assert scan.status_code==202,scan.text
        report=wait(client,'/api/attention/scans/'+scan.json()['scan_id'])['report']
        queue=client.get('/api/attention/queue').json()
        item=next(r['item'] for r in queue['items'] if r['item']['symbol']=='APP')
        originals={e['evidence_id'] for e in item['packet']['evidence']}
        assert originals=={'Eclosure-original-'+str(i) for i in range(6)}
        first=client.post('/api/research/tasks',json={'question':'APP 客户验收消息改变持仓逻辑吗？',
            'mode':'FAST','attention_item_id':item['item_id']})
        assert first.status_code==202,first.text
        fast=wait(client,'/api/research/tasks/'+first.json()['task_id'])
        assert fast['status']=='COMPLETE',fast['error']
        assert fast['action_decision']['decision_action']=='HOLD'
        assert originals <= {s['evidence_id'] for s in fast['context_manifest']['selected']}
        assert any(s['identity']=='OWNER_CONFIRMED_THESIS' for s in fast['sources'])
        facts=next(r.facts for r in app.state.fixture_model.requests if r.purpose=='research.assess')
        projected={s['evidence']['evidence_id']:s for s in facts['sources']}
        assert any(s['identity']=='OWNER_CONFIRMED_THESIS' for s in projected.values())
        assert fast['context_manifest']['context_bytes']<=12000
        original_sources={s['evidence']['evidence_id']:s['evidence'] for s in fast['sources']}
        for eid in originals:
            row=projected[eid];e=row['evidence'];stored=original_sources[eid]
            text=e.get('excerpt') or projected[row['excerpt_text_reference']]['evidence']['excerpt']
            title=e.get('title') or projected[row['title_text_reference']]['evidence']['title']
            assert text==stored['excerpt'] and title==stored['title']
            assert e['content_hash']==stored['content_hash'] and e['source_uri']==stored['source_uri']
            assert e['published_at']==stored['published_at']
            assert e['structured_data']['record']==stored['structured_data']['record']
        assert not any(c['name']=='sec.fundamentals' for c in fast['tool_calls'])
        frozen=client.get('/api/research/tasks/'+fast['task_id']+'/revisions/1').json()
        response=client.post('/api/research/tasks',json={'task_id':fast['task_id'],
            'question':'APP 深入核查客户验收和回款条件。','mode':'DEEP','model_profile_id':'sol-deep',
            'attention_item_id':item['item_id']})
        assert response.status_code==202,response.text
        deep=wait(client,'/api/research/tasks/'+fast['task_id'])
        assert deep['status']=='COMPLETE',deep['error']
        assert deep['task_id']==fast['task_id'] and deep['revision']==2
        assert deep['snapshot_hash']==fast['snapshot_hash'] and deep['cutoff']==fast['cutoff']
        assert deep['action_decision']['decision_action']==('HOLD' if no_gain else 'ADD')
        assert deep['investigation']['status']==('NO_NEW_EVIDENCE' if no_gain else 'NEW_EVIDENCE')
        assert deep['assessment']['judgment_change']==('UNRESOLVED' if no_gain else 'CHANGED')
        assert originals <= {s['evidence_id'] for s in deep['context_manifest']['selected']}
        financial=client.get('/api/research/tasks/'+deep['task_id']+'/valuation?revision=2').json()
        assert financial['metrics']['ttm_revenue']['value']=='500'
        assert financial['metrics']['ttm_operating_cash_flow']['value']=='92'
        sources={s['evidence']['evidence_id']:s for s in deep['sources']}
        assert all(s in sources for s in deep['action_decision']['source_ids'])
        with app.state.research_service.store._connect() as db:
            before=list(db.execute('SELECT * FROM research_task_turns ORDER BY revision'))
        model_calls=len(app.state.fixture_model.requests)
        result=client.post('/api/research/tasks/'+deep['task_id']+'/dated-valuation',json={
            'revision':2,'symbol':'APP','price':'10','comparison_price':'12.50','price_date':'2026-10-16',
            'session':'HYPOTHETICAL','kind':'USER_SUPPLIED_SCENARIO','source_basis':'HYPOTHETICAL',
            'source_note':'Self-defined fictional price pair; not copied Provider quotes',
            'local_calculation_rights_attested':True,'assumptions':{'unchanged_since_share_date':True,
                'entire_common_equity_at_input_class_price':True,'consolidated_income_available_to_common':True}})
        assert result.status_code==200,result.text
        value=result.json();delta=value['report']['scenario_difference']
        assert Decimal(delta['metrics']['reference_equity_value']['delta'])==-250
        assert Decimal(delta['metrics']['reference_ps']['delta'])==Decimal('-.5')
        assert value['calculation_receipt']['content_hash']==content_hash(value['report'])
        assert len(app.state.fixture_model.requests)==model_calls
        assert client.get('/api/research/tasks/'+deep['task_id']).json()==deep
        assert client.get('/api/research/tasks/'+deep['task_id']+'/revisions/1').json()==frozen
        with app.state.research_service.store._connect() as db:
            assert list(db.execute('SELECT * FROM research_task_turns ORDER BY revision'))==before
        assert report['screened_article_receipts']>=6
