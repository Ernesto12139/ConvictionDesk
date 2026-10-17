"""Action applicability and Fast/Deep information difference across company types."""
import asyncio
from datetime import datetime, timezone

import pytest

from market_agent.agent import Agent
from market_agent.news import NewsResult
from market_agent.persistence import Store
from market_agent.portfolio_entry import PortfolioEntry
from market_agent.provider import MarketDataResult
from market_agent.research_task import ResearchRequest, ResearchService, source_item
from market_agent.sec import SECSearchResult
from evals.convictiondesk import FACTS, DIRECTIONS, make_service


@pytest.mark.parametrize('symbol',['APP','AAPL','SMR'])
def test_company_specific_direction_without_fabricated_financial_numbers(tmp_path,symbol):
    service, model = make_service(tmp_path)
    task, previous, snapshot = service.begin(ResearchRequest(question=symbol+' 的变化影响持仓逻辑吗？',mode='FAST'))
    asyncio.run(service.execute(task,previous,snapshot))
    saved = service.tasks.get(task.task_id)
    assert saved.status=='COMPLETE',saved.error
    assert saved.action_decision.decision_action == DIRECTIONS[symbol]
    assert saved.action_decision.position_status == 'HELD'
    assert FACTS[symbol] in saved.action_decision.key_drivers
    assert saved.portfolio_context['position']['price'] is None
    assert len(model.requests)==2
    assert all(request.max_output_tokens==3072 for request in model.requests)
    assert not any('supplemental' in call.name for call in saved.tool_calls)


def test_deep_adds_validated_evidence_and_information_not_only_prose(tmp_path):
    service, model = make_service(tmp_path)
    task, previous, snapshot = service.begin(ResearchRequest(question='APP 的变化影响持仓逻辑吗？',mode='DEEP'))
    asyncio.run(service.execute(task,previous,snapshot))
    saved = service.tasks.get(task.task_id)
    assert saved.status=='COMPLETE',saved.error
    assert saved.investigation['status']=='NEW_EVIDENCE'
    assert set(saved.investigation['new_evidence_ids']) <= set(saved.assessment.investigation_effect.evidence_ids)
    assert len(model.requests)==3
    assert saved.assessment.judgment_change=='UNCHANGED'


def test_unheld_reduce_is_read_back_as_not_applicable(tmp_path):
    from market_agent.action_decision import action_readback
    service, _ = make_service(tmp_path)
    task, previous, snapshot = service.begin(ResearchRequest(question='SMR 的变化影响持仓逻辑吗？',mode='FAST'))
    asyncio.run(service.execute(task,previous,snapshot))
    saved = service.tasks.get(task.task_id)
    unheld = saved.model_copy(update={'portfolio_context':{'snapshot_id':'synthetic-unheld','position':None}})
    result = action_readback(saved.assessment,unheld)
    assert result.decision_action is None and result.decision_status=='NOT_APPLICABLE'


def test_frozen_pre_action_payload_readback_previous_revision_and_next_job(tmp_path):
    import json
    from pathlib import Path
    from fastapi.testclient import TestClient
    from evals.research_agent import create_oracle_app, J5
    frozen = json.loads(Path('evals/fixtures/research_agent/pre-v05-research-task.json').read_text(encoding='utf-8'))
    raw, fingerprint = frozen['payload'], frozen['payload_hash']
    payload = json.loads(raw)
    app = create_oracle_app(tmp_path)
    service = app.state.research_service
    with service.store._connect() as db:
        db.execute('INSERT INTO research_tasks VALUES (?,?,?,?,?,?)',
            (payload['task_id'],1,'COMPLETE',payload['updated_at'],fingerprint,raw))
        db.execute('INSERT INTO research_task_turns VALUES (?,?,?,?)',(payload['task_id'],1,fingerprint,raw))
    with TestClient(app) as client:
        restored = client.get('/api/research/tasks/'+payload['task_id']).json()
        assert restored['assessment']['core_judgment']==payload['assessment']['core_judgment']
        assert 'action' not in restored['assessment'] and 'action_decision' not in restored
        assert service.tasks.revision(payload['task_id'],1).mode=='DEEP'
        task,previous,snapshot = service.begin(ResearchRequest(question=J5,task_id=payload['task_id']))
        asyncio.run(service.execute(task,previous,snapshot))
        assert client.get('/api/research/tasks/'+task.task_id).json()['status']=='COMPLETE'
    with service.store._connect() as db:
        assert db.execute('SELECT payload_hash,payload FROM research_task_turns WHERE task_id=? AND revision=1',
            (payload['task_id'],)).fetchone()==(fingerprint,raw)
