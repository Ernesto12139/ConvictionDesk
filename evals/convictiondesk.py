"""Public synthetic decision oracle; never imports Owner state or credentials."""
import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from market_agent.agent import Agent
from market_agent.news import NewsResult
from market_agent.persistence import Store
from market_agent.portfolio_entry import PortfolioEntry
from market_agent.provider import MarketDataResult
from market_agent.research_task import ResearchRequest, ResearchService, source_item
from market_agent.sec import SECSearchResult
NOW = datetime(2026,10,17,4,tzinfo=timezone.utc)


FACTS = {'APP':'合成广告客户留存改善，电商需求仍待核查。',
         'AAPL':'合成设备收入稳定，服务业务增长，硬件更新需求仍待确认。',
         'SMR':'合成项目商业化延后，现金消耗持续，订单兑现尚未确认。'}
DIRECTIONS = {'APP':'ADD', 'AAPL':'HOLD', 'SMR':'REDUCE'}


class Market:
    def retrieve_eod(self, symbols, cutoff):
        return MarketDataResult(observations=[],evidence=[])


class Company:
    def search(self, symbol, *args):
        news = isinstance(symbol,list)
        symbol = symbol[0] if news else symbol
        receipt = source_item('news' if news else 'sec_filing','VERIFIED_FACT',
            {'text':FACTS[symbol],'kind':'news' if news else 'filing'}, NOW, symbol=symbol,
            title='合成经营资料').evidence.model_copy(update={'published_at':NOW,'excerpt':FACTS[symbol]})
        return NewsResult(evidence=[receipt]) if news else SECSearchResult(cik='synthetic',indexed_documents=1,evidence=[receipt])

    def targeted_search(self, symbol, *args):
        text = '合成公司补充披露合同客户验收完成，回款条件已满足。'
        receipt = source_item('sec_filing','VERIFIED_FACT',{'text':text},NOW,symbol=symbol,
            title='合成定向补查').evidence.model_copy(update={'published_at':NOW,'excerpt':text})
        return SECSearchResult(cik='synthetic',indexed_documents=0,evidence=[receipt])


class Model:
    model_name = 'synthetic-decision-oracle'

    def __init__(self):
        self.requests = []

    async def complete_structured(self, request):
        self.requests.append(request)
        if request.purpose == 'research.intent':
            return request.response_model(objective='REVIEW_THESIS',focus_symbol=request.facts['explicit_literals']['symbols'][0],
                clarification=None,sec_query='operating performance cash guidance',use_news=True,use_sec=True)
        if request.purpose == 'attention_deep_v1':
            # This request contains an AttentionItem, not ResearchTask sources.
            from market_agent.attention import DeepOutput
            facts = request.facts
            return DeepOutput(what_changed=facts['what_changed'],
                why_it_matters=facts['screen']['rationale'], thesis_impact='UNKNOWN',
                owner_attention='SOON', next_step='REVIEW', next_check={
                    'text':'合成深入核查：对照原始披露，核查经营变化与现金兑现条件。',
                    'evidence_ids':[e['evidence_id'] for e in facts['packet']['evidence']]})
        if request.purpose not in {'research.assess', 'research.reassess'}:
            from market_agent.llm import LLMError
            raise LLMError('configuration', 'Unsupported synthetic request purpose')
        sources = [row for row in request.facts['sources'] if row['identity']=='VERIFIED_FACT'
                   and row['evidence']['evidence_type'] in {'news','sec_filing'}]
        symbol = request.facts['symbol']
        ids = [sources[0]['evidence']['evidence_id']]
        statement = {'text':FACTS[symbol], 'identity':'AI_DRAFT','evidence_ids':ids}
        core = {**statement,'text':'倾向'+DIRECTIONS[symbol]+'；'+FACTS[symbol]}
        data = dict(disposition='RESEARCH_ONLY',core_judgment=core,key_reasons=[statement],support=[statement],
            counterevidence=[{**statement,'text':'合成披露中的需求持续性与现金兑现仍需核查。'}],
            action_conditions=[{**statement,'text':'若新增正式披露推翻当前经营驱动，应改变方向。'}],
            next_checks=[{**statement,'text':'核查客户验收、回款条件与当前经营进展。'}],
            action={'direction':DIRECTIONS[symbol],'status':'PROVISIONAL','evidence_strength':'LIMITED',
                'reason':core,'thesis_delta':{**statement,'identity':'UNKNOWN','text':'未提供确认的持仓论点；现有合成经营事实只能形成待确认的判断依据。'}})
        if request.facts['supplemental_allowed']:
            data['evidence_gap'] = {'tool':'SEC','query':'customer acceptance cash collection',
                'material_question':'合同客户是否完成验收并满足回款条件？','why_material':'回款兑现决定经营改善是否具有现金支持。',
                'expected_information':'取得合同客户验收与回款条件的正式披露。','evidence_ids':ids}
        if request.purpose == 'research.reassess':
            new = request.facts['investigation']['new_evidence_ids']
            data['investigation_effect'] = {'text':'补充披露确认客户验收和回款条件，新增了现金兑现依据，方向保持。',
                'identity':'AI_DRAFT','evidence_ids':new}
            data['judgment_change'] = 'UNCHANGED'
        return request.response_model.model_validate(data)


def make_service(tmp_path):
    store = Store(tmp_path/'synthetic.sqlite3')
    store.add_snapshot(PortfolioEntry(accounts=[{'account_id':'test','name':'Synthetic','cash':'1000'}],
        positions=[{'position_id':symbol,'account_id':'test','symbol':symbol,'quantity':'3','avg_cost':'100'}
                   for symbol in FACTS]).snapshot(NOW))
    model, company = Model(), Company()
    return ResearchService(Agent(store,Market(),model,news=company,sec=company,clock=lambda:NOW)), model



def create_app():
    from market_agent.main import create_app as application
    from market_agent.model_profiles import ModelProfiles
    folder = Path(os.environ['MIA_ISOLATED_ROOT'])
    folder.mkdir(parents=True,exist_ok=True)
    service,model = make_service(folder)
    profiles = ModelProfiles({'OPENAI_API_KEY':'synthetic-not-a-key'},client_factory=lambda profile:model)
    app = application(db_path=service.store.path,market_provider=Market(),llm_client=model,
        news_provider=Company(),sec_provider=Company(),model_profiles=profiles,clock=lambda:NOW,environment={})
    return app
