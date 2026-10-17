import asyncio
import json

import pytest
from pydantic import BaseModel

from market_agent.llm import LLMError, StructuredLLMRequest
from market_agent.model_profiles import ModelProfile, ModelProfiles
from market_agent.tdb import TDBError


class Answer(BaseModel):
    ok: bool


def request():
    return StructuredLLMRequest(purpose='synthetic',response_model=Answer,facts={},
        evidence_ids=[],numeric_fact_tokens=[],generation_mode='generate')


def test_allowlist_mode_and_private_key_readback():
    registry = ModelProfiles({'OPENAI_API_KEY':'SYNTHETIC_PRIVATE_KEY'})
    assert 'SYNTHETIC_PRIVATE_KEY' not in str(registry.describe())
    assert registry.resolve('sol-fast').mode=='FAST'
    with pytest.raises(TDBError):
        registry.resolve('arbitrary-provider-model')
    with pytest.raises(TDBError):
        registry.resolve('sol-fast','DEEP')
    with pytest.raises(TDBError):
        ModelProfiles({}).resolve('sol-fast')


def test_explicit_one_fallback_and_actual_model_identity():
    calls = []
    profiles = [ModelProfile('primary','FAST',max_output_tokens=1024,timeout_seconds=30,fallback_id='secondary'),
                ModelProfile('secondary','FAST',max_output_tokens=1024,timeout_seconds=30)]

    class Client:
        model_name = 'synthetic-executed-model'

        def __init__(self,profile):
            self.profile = profile

        async def complete_structured(self, req):
            calls.append((self.profile.profile_id,req.max_output_tokens))
            if self.profile.profile_id=='primary':
                raise LLMError('provider_unavailable','synthetic failure')
            return Answer(ok=True)

        def call_metadata(self):
            return {'actual_model':self.model_name,'usage':{'input_tokens':1,'output_tokens':1}}

    registry = ModelProfiles({'OPENAI_API_KEY':'synthetic'},profiles=profiles,client_factory=Client)
    client = registry.client('primary')
    assert asyncio.run(client.complete_structured(request())).ok
    assert calls == [('primary',1024),('secondary',1024)]
    assert client.call_metadata()['actual_model']=='synthetic-executed-model'
    assert len(json.loads(client.call_metadata()['context_metrics']['profile_attempts']))==2


def test_profile_executes_complete_research_with_persisted_actual_identity(tmp_path):
    from test_convictiondesk_decision import make_service
    from market_agent.research_task import ResearchRequest
    service, model = make_service(tmp_path)
    service.model_profiles = ModelProfiles({'OPENAI_API_KEY':'synthetic'},client_factory=lambda profile:model)
    task,previous,snapshot = service.begin(ResearchRequest(question='APP 的变化影响持仓逻辑吗？',model_profile_id='sol-fast'))
    asyncio.run(service.execute(task,previous,snapshot))
    saved = service.tasks.get(task.task_id)
    assert saved.status=='COMPLETE',saved.error
    assert saved.mode=='FAST' and saved.model_profile_id=='sol-fast'
    assert all(event.model==model.model_name for event in saved.model_events)
    assert all(event.context_metrics['model_profile']=='sol-fast' for event in saved.model_events)
    followup,_,_ = service.begin(ResearchRequest(question='那下次财报要看什么？',task_id=task.task_id))
    assert followup.mode=='FAST' and followup.model_profile_id=='sol-fast'


def test_validation_error_never_triggers_fallback():
    calls=[]
    class Invalid:
        model_name='synthetic'
        async def complete_structured(self, req):
            calls.append(True)
            raise LLMError('unsupported_numeric_fact','synthetic validation failure')
    registry = ModelProfiles({'OPENAI_API_KEY':'synthetic'},profiles=[
        ModelProfile('primary','FAST',max_output_tokens=1024,timeout_seconds=30,fallback_id='secondary'),
        ModelProfile('secondary','FAST',max_output_tokens=1024,timeout_seconds=30)],
        client_factory=lambda profile:Invalid())
    with pytest.raises(LLMError,match='unsupported_numeric_fact'):
        asyncio.run(registry.client('primary').complete_structured(request()))
    assert len(calls)==1


def test_environment_startup_exposes_real_profiles_and_rejects_unknown_before_task_creation(tmp_path):
    from fastapi.testclient import TestClient
    from market_agent.main import app_from_environment
    app = app_from_environment({'MIA_DB_PATH':str(tmp_path/'profiles.sqlite3'),'OPENAI_API_KEY':'synthetic-key'})
    with TestClient(app) as client:
        profiles = client.get('/api/research/model-profiles')
        assert 'synthetic-key' not in profiles.text
        assert {p['profile_id'] for p in profiles.json()['profiles']}=={'sol-fast','sol-deep'}
        assert client.post('/api/research/tasks',json={'question':'APP 如何？','model_profile_id':'invented'}).status_code==422
        assert client.get('/api/research/tasks').json()==[]
    other = app_from_environment({'MIA_DB_PATH':str(tmp_path/'other.sqlite3'),'OPENAI_MODEL':'different-model'})
    assert other.state.research_service.model_profiles is None
