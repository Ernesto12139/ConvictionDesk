"""Fictional evidence-to-action journey on production APIs, with scripted judgments.

Every number, issuer event and owner confirmation here is synthetic. This tests
information delivery and immutable records, never real-model investment quality.
"""
from datetime import timedelta
from pathlib import Path
import os

from evals import v09_intelligence as fixture
from market_agent.tdb import content_hash
from market_agent.news import NewsResult
from market_agent.sec import SECSearchResult

NOW=fixture.NOW
THESIS='只有客户留存改善并兑现合同回款，才考虑增加已有持仓；单有验收消息不足以加仓。'


def financials(symbol):
    rows=[]
    for e in fixture.financial_evidence(symbol):
        if e.structured_data.get('share_facts'):
            data={**e.structured_data,'share_facts':[{**r,'accession_number':'0000000001-26-000001'}
                for r in e.structured_data['share_facts']],
                'issuer_listing_identity':{'tickers':[symbol],'observed_at':fixture.AVAILABLE.isoformat(),
                    'source_uri':'https://example.com/fictional-listing'}}
            e=e.model_copy(update={'structured_data':data,'content_hash':content_hash(data)})
        rows.append(e)
    return rows


def originals(symbol):
    if symbol=='SMR':
        return fixture.Company().search([symbol]).evidence
    rows=[]
    for index in range(6):
        data={'text':'合成公司披露客户验收完成，合同回款条件仍待正式确认。',
            'symbol':symbol,'synthetic':True,'record':index,'published_at_precision':'INSTANT'}
        e=fixture.receipt('news',symbol,data,'Orchid customer contract acceptance completed awaiting cash collection confirmation',
            published=NOW-timedelta(minutes=30+index))
        e=e.model_copy(update={'evidence_id':'Eclosure-original-'+str(index),
            'source_uri':f'https://example.com/closure-outlet-{index}/acceptance'})
        rows.append(e)
    return rows


class Company(fixture.Company):
    def search(self,symbol,*args):
        if isinstance(symbol,list):
            return NewsResult(evidence=[e for s in symbol for e in originals(s)],coverage_complete=True)
        if symbol=='SMR':
            return fixture.Company().search(symbol,*args)
        e=fixture.receipt('sec_filing',symbol,{'symbol':symbol,'synthetic':True,
            'text':'合成广告客户留存改善，电商需求持续性未知；客户验收和合同回款尚待确认。'},'初始经营披露')
        return SECSearchResult(cik='synthetic',indexed_documents=0,evidence=[e])

    def fundamentals(self,symbol,cutoff):return financials(symbol)


class Market(fixture.Market):
    def retrieve_eod(self,symbols,cutoff):
        result=super().retrieve_eod(symbols,cutoff)
        sources=[fixture.qualified_price(e,financials(e.symbol)) for e in result.evidence]
        return result.model_copy(update={'evidence':sources})


class Model(fixture.Model):
    async def complete_structured(self,request):
        output=await super().complete_structured(request)
        if request.purpose not in {'research.assess','research.reassess'} or request.facts['symbol']!='APP':
            return output
        from market_agent.research_task import ResearchStatement
        def statement(text,ids,identity='AI_DRAFT'):
            return ResearchStatement(text=text,identity=identity,evidence_ids=ids)
        sources=request.facts['sources']
        originals_ids=[s['evidence']['evidence_id'] for s in sources
            if s['evidence']['evidence_id'].startswith('Eclosure-original-')]
        event_ids=originals_ids or [s['evidence']['evidence_id'] for s in sources
            if s['identity']=='VERIFIED_FACT' and s['evidence']['evidence_type']=='sec_filing'][:1]
        thesis_ids=[s['evidence']['evidence_id'] for s in sources if s['identity']=='OWNER_CONFIRMED_THESIS']
        new_ids=(request.facts.get('investigation') or {}).get('new_evidence_ids',[])
        gained=request.purpose=='research.reassess' and bool(new_ids)
        direction='ADD' if gained else 'HOLD'
        text=('暂定 ADD：验收后补充披露确认回款条件，满足原投资逻辑中的现金兑现条件；电商需求持续性仍待核查。'
            if gained else '暂定 HOLD：验收完成是新变化，但合同回款条件尚未确认，现有持仓先等待关键证据。')
        core=statement(text,new_ids if gained else event_ids)
        thesis=statement('原已确认投资逻辑要求客户留存与回款兑现。'+
            ('新披露满足回款条件，支持重新考虑加仓；未证明未来需求持续。' if gained else '验收消息尚不足以证明回款兑现，暂不满足加仓条件。'),
            list(dict.fromkeys([*thesis_ids,*(new_ids if gained else event_ids)])))
        fields={'core_judgment':core,'key_reasons':[core,*output.key_reasons[:1]],
            'support':[core,*output.support[:1]],
            'counterevidence':[statement('客户验收或回款条件不证明电商需求可以持续，仍须核查后续正式经营披露。',event_ids)],
            'action_conditions':[statement('若正式披露撤回客户验收或否认回款条件，应重新评估加仓方向；价格变低本身不能满足经营条件。',event_ids)],
            'next_checks':[statement('核查客户回款条件及后续电商需求，分清合同验收与实际回款。',event_ids)],
            'action':output.action.model_copy(update={'direction':direction,'reason':core,'thesis_delta':thesis})}
        if gained:fields.update(investigation_effect=statement('新增正式披露确认客户验收及回款条件，补足原逻辑中的回款资格缺口。',new_ids),judgment_change='CHANGED')
        return output.model_copy(update=fields)


def create_app():
    from market_agent.main import create_app as application
    from market_agent.persistence import Store
    from market_agent.portfolio_entry import PortfolioEntry
    from market_agent.model_profiles import ModelProfiles
    from market_agent.research_universe import UniverseChange
    from market_agent.thesis import ThesisProfile
    folder=Path(os.environ['MIA_ISOLATED_ROOT']);folder.mkdir(parents=True,exist_ok=True)
    store=Store(folder/'closure-fixture.sqlite3')
    if store.get_snapshot() is None:
        store.add_snapshot(PortfolioEntry(accounts=[{'account_id':'fictional','name':'闭环合成验收','cash':'1000'}],
            positions=[{'position_id':'fictional-APP','account_id':'fictional','symbol':'APP','quantity':'3','avg_cost':'20'}]).snapshot(NOW))
    if store.get_thesis('APP',NOW) is None:
        original=fixture.receipt('news','APP',{'text':THESIS,'synthetic':True},'合成用户明确确认的逻辑',published=fixture.AVAILABLE)
        sid=original.evidence_id
        profile=ThesisProfile(symbol='APP',thesis_version_id='closure-fictional-confirmed',
            as_of=fixture.AVAILABLE,status='USER_CONFIRMED',generation={'model':'scripted-fixture-owner-confirmation'},
            core_drivers=[{'text':THESIS,'evidence_ids':[sid]}],key_assumptions=[{'text':'合同回款条件待确认。','evidence_ids':[sid]}],
            execution_risks=[{'text':'电商需求持续性未知。','evidence_ids':[sid]}],
            invalidation_conditions=[{'text':'正式披露否认回款条件。','evidence_ids':[sid]}],evidence_ids=[sid])
        evidence=original.model_copy(update={'evidence_type':'thesis','source_record_id':profile.thesis_version_id,
            'structured_data':profile.model_dump(mode='json'),'content_hash':content_hash(profile.model_dump(mode='json')),
            'retrieved_at':fixture.AVAILABLE,'as_of':fixture.AVAILABLE})
        store.add_thesis(profile,evidence)
    model=Model();company=Company(no_gain=os.environ.get('MIA_FIXTURE_NO_GAIN')=='1')
    profiles=ModelProfiles({'OPENAI_API_KEY':'synthetic-not-a-real-key'},client_factory=lambda profile:model)
    app=application(db_path=store.path,market_provider=Market(),llm_client=model,news_provider=company,
        sec_provider=company,model_profiles=profiles,clock=lambda:NOW,environment={})
    app.state.attention_service.research.change(UniverseChange(symbol='SMR',state='WATCHING'),NOW)
    app.state.fixture_model=model
    return app
