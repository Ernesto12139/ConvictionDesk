"""Explicitly fictional two-industry oracles for the production research journey.

These figures and scripted judgments test plumbing/guards, not investment quality.
No live source, external model, Owner database or credential is used.
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from evals.convictiondesk import Company as BaseCompany, Model as BaseModel
from market_agent.domain import EvidenceItem, PriceObservation
from market_agent.news import NewsResult
from market_agent.provider import MarketDataResult
from market_agent.sec import SECSearchResult
from market_agent.tdb import content_hash

NOW=datetime(2026,10,17,12,tzinfo=timezone.utc)
AVAILABLE=NOW-timedelta(days=7)
PERIODS=[('2025-10-01','2025-12-31'),('2026-01-01','2026-03-31'),
         ('2026-04-01','2026-06-30'),('2026-07-01','2026-09-30')]


def receipt(kind,symbol,payload,title,*,provider='synthetic',published=AVAILABLE,asof=None):
    digest=content_hash({'symbol':symbol,**payload})
    return EvidenceItem(evidence_id='E'+digest[7:23],evidence_type=kind,provider=provider,
        symbol=symbol,title='冻结合成 · '+title,source_uri='https://example.com/v09-fixture/'+symbol,
        source_record_id=digest,published_at=published,as_of=asof,retrieved_at=NOW,
        content_hash=content_hash(payload),excerpt=payload.get('text'),structured_data=payload)


def financial_evidence(symbol):
    loss=symbol=='SMR'  # Fixture only; production has no ticker-based strategy.
    parent=receipt('sec_filing',symbol,{'symbol':symbol,'synthetic':True,'metrics':[],'share_facts':[
        {'metric':'shares_outstanding','concept':'us-gaap:CommonStockSharesOutstanding',
         'value':'200' if loss else '100','unit':'shares','start':None,'end':'2026-09-30',
         'available_at':AVAILABLE.isoformat(),'scope':'ENTIRE_FILING_ENTITY'}]},
        '报告股数（虚构）',provider='sec_edgar')
    result=[parent]
    for n,(start,end) in enumerate(PERIODS):
        values={'revenue':'0' if loss else str(110+10*n),
            'net_income':str(-10-n) if loss else str(12+n),
            'operating_cash_flow':str(-20-2*n) if loss else str(20+2*n),
            'diluted_eps':'-0.2' if loss else str(Decimal('.5')+Decimal('.05')*n)}
        rows=[]
        for name,value in values.items():
            row={'metric':name,'concept':name,'value':value,'unit':'USD/shares' if name=='diluted_eps' else 'USD',
                 'start':start,'end':end,'available_at':AVAILABLE.isoformat()}
            rows.append({**row,'basis':'REPORTED','inputs':[{**row,'accession_number':'synthetic-only'}]})
        data={'calculation':'sec_quarterly_v1','symbol':symbol,'quarter':{'start':start,'end':end,'metrics':rows},
              'source_evidence_ids':[parent.evidence_id]}
        result.append(receipt('calculation',symbol,data,'季度数据（虚构）',provider='python_decimal',
                      asof=datetime.fromisoformat(end+'T00:00:00+00:00')))
    return result


def qualified_price(evidence,financials):
    """Fictional complete corporate-action/class fixture, never a live check."""
    shares=next(row for e in financials for row in [*e.structured_data.get('share_facts',[]),
        *e.structured_data.get('metrics',[])] if row.get('metric')=='shares_outstanding')
    payload={**evidence.structured_data,'price_basis':'unadjusted','capital_basis_check':{
        'from_date':'2025-10-01','through_date':'2026-10-16','known_at':NOW.isoformat(),
        'share_count_from_date':shares['end'],'coverage_complete':True,'split_status':'NO_CHANGE',
        'price_value':evidence.structured_data['price'],'price_as_of':evidence.as_of.isoformat(),
        'price_session':evidence.structured_data.get('session') or 'COMPLETED_CLOSE',
        'share_count_status':'NO_CHANGE','share_class_alignment':'VERIFIED',
        'source_uri':'https://example.com/synthetic-corporate-actions',
        'input_hashes':{e.evidence_id:e.content_hash for e in financials}}}
    return evidence.model_copy(update={'structured_data':payload,'content_hash':content_hash(payload)})


class Market:
    def retrieve_eod(self,symbols,cutoff):
        observations=[];sources=[]
        for symbol in symbols:
            if symbol not in {'APP','SMR'}:continue
            stamp=datetime(2026,10,16,20,tzinfo=timezone.utc)
            value='8' if symbol=='SMR' else '23'
            evidence=receipt('market_data',symbol,{'symbol':symbol,'price':value,'currency':'USD',
                'session':'COMPLETED_CLOSE','session_date':'2026-10-16'},'日终收盘（虚构）',published=None,asof=stamp)
            evidence=qualified_price(evidence,financial_evidence(symbol))
            sources.append(evidence)
            observations.append(PriceObservation(symbol=symbol,price=Decimal(value),as_of=stamp,
                                provider='synthetic',evidence_id=evidence.evidence_id))
        return MarketDataResult(observations=observations,evidence=sources)


class Company(BaseCompany):
    def __init__(self,no_gain=False):
        self.no_gain=no_gain

    def search(self,symbol,*args):
        news=isinstance(symbol,list);symbol=symbol[0] if news else symbol
        text=('合成项目商业化延后，现金消耗持续，订单兑现尚未确认。' if symbol=='SMR' else
              '合成广告客户留存改善，电商需求仍待核查。')
        data={'text':text,'symbol':symbol,'synthetic':True,'published_at_precision':'INSTANT',
              'kind':'news' if news else 'initial-filing'}
        evidence=receipt('news' if news else 'sec_filing',symbol,data,'经营变化（虚构）',published=NOW-timedelta(hours=1))
        return NewsResult(evidence=[evidence],coverage_complete=True) if news else SECSearchResult(cik='synthetic',indexed_documents=0,evidence=[evidence])

    def fundamentals(self,symbol,cutoff):
        return financial_evidence(symbol)

    def targeted_search(self,symbol,*args):
        if self.no_gain:return self.search(symbol,*args)
        text='合成公司补充披露合同客户验收完成，回款条件已满足。'
        evidence=receipt('sec_filing',symbol,{'text':text,'symbol':symbol,'synthetic':True},
                         '新增回款条件（虚构）',published=NOW-timedelta(minutes=20))
        return SECSearchResult(cik='synthetic',indexed_documents=0,evidence=[evidence])


class Model(BaseModel):
    model_name='synthetic-v09-scripted-oracle'

    async def complete_structured(self,request):
        if request.purpose=='attention_screen_v1':
            self.requests.append(request)
            from market_agent.attention import ScreenBatch
            rows=[]
            for packet in request.facts['events']:
                evidence=packet['evidence'][0]
                statement={'text':evidence['excerpt'],'evidence_ids':[evidence['evidence_id']]}
                rows.append({'event_key':packet['event_key'],'materiality':'MEDIUM','direction':'MIXED',
                    'thesis_impact':'UNKNOWN','urgency':'SOON','evidence_confidence':'MEDIUM',
                    'rationale':statement,'brief':{'headline':statement,'next_check':{
                        'text':'对照经营变化、现金兑现和适用财务依据。','evidence_ids':[evidence['evidence_id']]}}})
            return ScreenBatch.model_validate({'events':rows})
        # The existing oracle assumes new supplemental evidence. Handle the
        # genuine deduplicated/no-gain control explicitly without inventing refs.
        no_gain=request.purpose=='research.reassess' and not request.facts['investigation']['new_evidence_ids']
        if no_gain:
            before=request.model_copy(update={'purpose':'research.assess',
                'facts':{**request.facts,'supplemental_allowed':False}})
            output=await super().complete_structured(before)
            self.requests[-1]=request
            output=output.model_copy(update={'investigation_effect':output.core_judgment.model_copy(update={
                'text':'本次补查没有取得新增证据，原有现金兑现缺口仍未解决。','identity':'UNKNOWN'}),
                'judgment_change':'UNRESOLVED'})
        else:
            output=await super().complete_structured(request)
        # Only Research assessments have a sources/valuation contract. Attention
        # deep already returned its own validated DeepOutput from the oracle.
        if request.purpose not in {'research.assess','research.reassess'}:return output
        derived=next((row['evidence'] for row in request.facts['sources']
                      if row['evidence']['structured_data'].get('calculation')=='task_valuation_v1'),None)
        if not derived:return output
        from market_agent.research_task import ResearchStatement
        metrics=derived['structured_data']['metrics'];source_id=derived['evidence_id']
        earnings=metrics['ttm_diluted_eps']['value']
        loss=Decimal(earnings if earnings is not None else metrics['ttm_net_income']['value'])<0
        text=(f"历史 TTM 净利润 {metrics['ttm_net_income']['value']} USD；现金消耗与商业化兑现需要优先核查。" if loss else
              f"历史 TTM 营收 {metrics['ttm_revenue']['value']} USD；经营现金流 {metrics['ttm_operating_cash_flow']['value']} USD。")
        fact=ResearchStatement(text=text,identity='AI_DRAFT',evidence_ids=[source_id])
        fields={'key_reasons':[fact,*output.key_reasons[:2]],'support':[fact,*output.support[:2]]}
        if loss:
            action=output.action.model_copy(update={'direction':None,'status':'UNDETERMINED','evidence_strength':'INSUFFICIENT',
                'reason':output.action.reason.model_copy(update={'text':'尚不足以判断应投入资金；亏损与商业化兑现缺口需要先核查。'})})
            fields.update(action=action,core_judgment=output.core_judgment.model_copy(update={
                'text':'方向未形成：亏损和商业化兑现缺口尚未解决，不以不适用的市盈率推断便宜。'}))
        return output.model_copy(update=fields)


def create_app(*,capital_reference_dir=None):
    from market_agent.main import create_app as application
    from market_agent.persistence import Store
    from market_agent.portfolio_entry import PortfolioEntry
    from market_agent.model_profiles import ModelProfiles
    from market_agent.research_universe import UniverseChange
    folder=Path(os.environ['MIA_ISOLATED_ROOT']);folder.mkdir(parents=True,exist_ok=True)
    store=Store(folder/'fixture.sqlite3')
    if store.get_snapshot() is None:
        store.add_snapshot(PortfolioEntry(accounts=[{'account_id':'synthetic','name':'冻结合成验收','cash':'1000'}],
            positions=[{'position_id':'synthetic-APP','account_id':'synthetic','symbol':'APP','quantity':'3','avg_cost':'20'}]).snapshot(NOW))
    model=Model();company=Company(no_gain=os.environ.get('MIA_FIXTURE_NO_GAIN')=='1')
    profiles=ModelProfiles({'OPENAI_API_KEY':'synthetic-not-a-real-key'},client_factory=lambda profile:model)
    display_environment=({'MIA_CAPITAL_REFERENCE_MODE':'personal-display',
        'MIA_CAPITAL_REFERENCE_DIR':str(capital_reference_dir)} if capital_reference_dir is not None else {})
    app=application(db_path=store.path,market_provider=Market(),llm_client=model,news_provider=company,
        sec_provider=company,model_profiles=profiles,clock=lambda:NOW,environment=display_environment)
    app.state.attention_service.research.change(UniverseChange(symbol='SMR',state='WATCHING'),NOW)
    app.state.fixture_model=model
    return app
