"""Synthetic, cross-industry valuation oracles; no live/provider/model requests."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json

from fastapi.testclient import TestClient
import pytest

from market_agent.domain import EvidenceItem
from market_agent.financial_series import common_shares
from market_agent.main import create_app
from market_agent.research_task import ResearchSource, ResearchTask
from market_agent.tdb import content_hash
from market_agent.valuation import valuation_evidence, valuation_report

NOW = datetime(2026,10,17,12,tzinfo=timezone.utc)
AVAILABLE = datetime(2026,10,10,12,tzinfo=timezone.utc)
PERIODS = [('2025-10-01','2025-12-31'),('2026-01-01','2026-03-31'),
           ('2026-04-01','2026-06-30'),('2026-07-01','2026-09-30')]


def source(symbol, kind, provider, payload, eid, *, as_of=None, published_at=AVAILABLE):
    e = EvidenceItem(evidence_id=eid,evidence_type=kind,provider=provider,symbol=symbol,
        source_uri='https://example.com/synthetic/'+eid,content_hash=content_hash(payload),
        as_of=as_of,published_at=published_at,retrieved_at=NOW,structured_data=payload)
    return ResearchSource(identity='VERIFIED_FACT',evidence=e,freshness='CURRENT')


def task(symbol='APP', *, loss=False):
    price_time = datetime(2026,10,16,20,tzinfo=timezone.utc)
    price = source(symbol,'market_data','synthetic',{'symbol':symbol,'price':'12.50',
        'currency':'USD','session_date':'2026-10-16'},'Eprice',as_of=price_time,published_at=None)
    shares = source(symbol,'sec_filing','sec_edgar',{'symbol':symbol,'metrics':[
        {'metric':'shares_outstanding','concept':'us-gaap:CommonStockSharesOutstanding',
         'value':'100','unit':'shares','start':None,'end':'2026-09-30',
         'available_at':AVAILABLE.isoformat(),'scope':'ENTIRE_FILING_ENTITY'}]},'Eshares')
    sources = [price,shares]
    for n,(start,end) in enumerate(PERIODS):
        values = {'revenue':str(100*(n+1)) if not loss else '0',
                  'net_income':str(10*(n+1)) if not loss else '-10',
                  'operating_cash_flow':str(15+10*n),
                  'diluted_eps':str([Decimal('.5'),Decimal('.6'),Decimal('.7'),Decimal('.7')][n]) if not loss else '-.25'}
        rows = [{'metric':name,'concept':name,'value':value,
                 'unit':'USD/shares' if name=='diluted_eps' else 'USD',
                 'start':start,'end':end,'available_at':AVAILABLE.isoformat(),'basis':'REPORTED',
                 'inputs':[{'metric':name,'start':start,'end':end,'value':value,
                            'unit':'USD/shares' if name=='diluted_eps' else 'USD',
                            'available_at':AVAILABLE.isoformat(),'accession_number':'synthetic'}]}
                for name,value in values.items()]
        data = {'calculation':'sec_quarterly_v1','symbol':symbol,
                'quarter':{'start':start,'end':end,'metrics':rows},'source_evidence_ids':['Eshares']}
        sources.append(source(symbol,'calculation','python_decimal',data,'Eq'+str(n),
                              as_of=datetime.fromisoformat(end).replace(tzinfo=timezone.utc)))
    from evals.v09_intelligence import qualified_price
    sources[0]=sources[0].model_copy(update={'evidence':qualified_price(sources[0].evidence,[s.evidence for s in sources[1:]])})
    return ResearchTask(task_id='synthetic-'+symbol,revision=1,status='COMPLETE',question='Synthetic valuation',
        focus_symbol=symbol,cutoff=NOW,updated_at=NOW,sources=sources)


def changed_source(t, index, transform):
    sources=list(t.sources);old=sources[index];data=deepcopy(old.evidence.structured_data)
    transform(data)
    sources[index]=old.model_copy(update={'evidence':old.evidence.model_copy(update={
        'structured_data':data,'content_hash':content_hash(data)})})
    return t.model_copy(update={'sources':sources})


def test_software_issuer_exact_ttm_market_cap_and_multiples_with_lineage():
    report=valuation_report(task('APP'))
    assert report.metrics['ttm_revenue'].value==Decimal('1000')
    assert report.metrics['ttm_operating_cash_flow'].value==Decimal('120')
    assert report.metrics['ttm_diluted_eps'].value==Decimal('2.5')
    assert report.metrics['market_cap_reference'].value==Decimal('1250')
    assert report.metrics['pe_ttm'].value==Decimal('5')
    assert report.metrics['price_sales_ttm'].value==Decimal('1.25')
    assert report.metrics['market_cap_reference'].status=='REFERENCE'
    assert report.metrics['market_cap_reference'].evidence_ids==['Eprice','Eshares']
    assert report.price_reference['session']=='COMPLETED_CLOSE'
    assert report.price_reference['feed_delay']=='UNKNOWN'
    assert report.shares_reference['end']=='2026-09-30'
    assert report.ttm_period=={'start':'2025-10-01','end':'2026-09-30'}
    assert report.metrics['enterprise_value'].status=='UNKNOWN'


def test_pre_revenue_energy_issuer_has_no_negative_or_zero_denominator_multiple():
    report=valuation_report(task('SMR',loss=True))
    assert report.symbol=='SMR' and report.metrics['market_cap_reference'].value==Decimal('1250')
    assert report.metrics['pe_ttm'].status=='NOT_MEANINGFUL'
    assert report.metrics['price_sales_ttm'].status=='NOT_MEANINGFUL'
    assert report.metrics['pe_ttm'].value is None


def test_missing_shares_price_currency_and_issuer_do_not_become_zero():
    original=task()
    for index,transform in [(1,lambda d:d.update(metrics=[])),
                            (0,lambda d:d.update(currency='CAD')),
                            (0,lambda d:d.update(symbol='SMR'))]:
        report=valuation_report(changed_source(original,index,transform))
        assert report.metrics['market_cap_reference'].value is None
        assert report.metrics['market_cap_reference'].status=='UNKNOWN'
    # A user assertion is never a replacement market price.
    assertion=original.sources[0].model_copy(update={'identity':'USER_ASSERTION'})
    assert valuation_report(original.model_copy(update={'sources':[assertion,*original.sources[1:]]})).price_reference is None


@pytest.mark.parametrize('change', ['future','stale','nan','unit','scope'])
def test_share_inputs_need_time_unit_scope_and_finite_value(change):
    def transform(d):
        row=d['metrics'][0]
        if change=='future':row['available_at']='2027-01-01T00:00:00+00:00'
        elif change=='stale':row['end']='2025-01-01'
        elif change=='nan':row['value']='NaN'
        elif change=='unit':row['unit']='USD'
        else:row['scope']='DILUTED_WEIGHTED_AVERAGE'
    assert valuation_report(changed_source(task(),1,transform)).metrics['market_cap_reference'].status=='UNKNOWN'


def test_gap_ytd_eps_and_conflicting_restatement_basis_remain_unknown():
    t=task();assert valuation_report(t.model_copy(update={'sources':t.sources[:-1]})).metrics['ttm_revenue'].value is None
    def ytd(d):
        next(r for r in d['quarter']['metrics'] if r['metric']=='diluted_eps')['basis']='YTD_DIFFERENCE'
    assert valuation_report(changed_source(t,2,ytd)).metrics['pe_ttm'].status=='UNKNOWN'
    def conflict(d):
        r=next(r for r in d['quarter']['metrics'] if r['metric']=='revenue')
        r['inputs'].append({'metric':'revenue','start':PERIODS[0][0],'end':PERIODS[0][1],
                            'value':'101','unit':'USD','available_at':AVAILABLE.isoformat()})
    assert valuation_report(changed_source(t,3,conflict)).metrics['ttm_revenue'].status=='UNKNOWN'


def test_global_decimal_precision_does_not_change_authoritative_results():
    with localcontext() as context:
        context.prec=3
        report=valuation_report(task())
    assert report.metrics['market_cap_reference'].value==Decimal('1250')
    assert report.metrics['price_sales_ttm'].value==Decimal('1.25')


def test_future_parent_cannot_qualify_a_past_dated_quarter():
    t=task();sources=list(t.sources)
    sources[1]=sources[1].model_copy(update={'evidence':sources[1].evidence.model_copy(update={
        'published_at':NOW.replace(year=2027),'retrieved_at':NOW.replace(year=2027)})})
    report=valuation_report(t.model_copy(update={'sources':sources}))
    assert report.metrics['ttm_revenue'].status=='UNKNOWN'
    assert report.metrics['pe_ttm'].status=='UNKNOWN'


def test_reported_amount_must_match_its_exact_original_input():
    def change(d):
        next(r for r in d['quarter']['metrics'] if r['metric']=='revenue')['value']='999'
    report=valuation_report(changed_source(task(),2,change))
    assert report.metrics['ttm_revenue'].status=='UNKNOWN'


def test_missing_concept_is_an_unknown_not_an_api_exception():
    def change(d):
        next(r for r in d['quarter']['metrics'] if r['metric']=='revenue').pop('concept')
    report=valuation_report(changed_source(task(),2,change))
    assert report.metrics['ttm_revenue'].status=='UNKNOWN'


def test_conflicting_price_receipts_at_same_time_are_not_arbitrarily_chosen():
    t=task(); second=changed_source(t,0,lambda d:d.update(price='99')).sources[0]
    second=second.model_copy(update={'evidence':second.evidence.model_copy(update={'evidence_id':'Eprice-conflict'})})
    report=valuation_report(t.model_copy(update={'sources':[*t.sources,second]}))
    assert report.price_reference is None
    assert report.metrics['market_cap_reference'].status=='UNKNOWN'


def test_each_valuation_number_carries_currency_period_asof_and_source_status():
    report=valuation_report(task())
    for key,row in report.model_dump(mode='json')['metrics'].items():
        assert {'value','unit','currency','period','as_of','source_id','data_status'} <= row.keys(),key
        assert row['data_status']==row['status']
        if row['value'] is not None:
            assert row['as_of'] and row['source_id'] and row['period']
    assert report.metrics['ttm_revenue'].currency=='USD'
    assert report.metrics['pe_ttm'].currency is None


@pytest.mark.parametrize('change',['absent','split','issuance','class','incomplete','future','hash','untrusted'])
def test_unqualified_capital_basis_cannot_reach_market_cap_or_multiples(change):
    def transform(d):
        check=d['capital_basis_check']
        if change=='absent':d.pop('capital_basis_check')
        elif change=='split':check['split_status']='CHANGE_DETECTED';d['price']='1.25'
        elif change=='issuance':check['share_count_status']='CHANGE_DETECTED'
        elif change=='class':check['share_class_alignment']='MISMATCH'
        elif change=='incomplete':check['coverage_complete']=False
        elif change=='future':check['known_at']='2027-01-01T00:00:00+00:00'
        elif change=='hash':check['input_hashes']['Eshares']='sha256:wrong'
    t=changed_source(task(),0,transform)
    if change=='untrusted':
        sources=list(t.sources);sources[0]=sources[0].model_copy(update={
            'evidence':sources[0].evidence.model_copy(update={'provider':'untrusted-provider'})})
        t=t.model_copy(update={'sources':sources})
    report=valuation_report(t)
    assert report.capital_basis_status!='VERIFIED_NO_CHANGE'
    assert all(report.metrics[key].status=='UNKNOWN' for key in ['market_cap_reference','pe_ttm','price_sales_ttm'])
    assert report.metrics['ttm_revenue'].value==Decimal('1000')


def test_later_overnight_quote_does_not_replace_completed_close():
    t=task();late=changed_source(t,0,lambda d:d.update(price='99',session='OVERNIGHT',session_date='2026-10-17')).sources[0]
    late=late.model_copy(update={'evidence':late.evidence.model_copy(update={
        'evidence_id':'Eovernight','as_of':NOW})})
    report=valuation_report(t.model_copy(update={'sources':[*t.sources,late]}))
    assert report.price_reference['evidence_id']=='Eprice'
    assert report.metrics['pe_ttm'].value==Decimal('5')


def test_citeable_valuation_survives_actual_context_projection_and_numeric_guard():
    from market_agent.research_context import build_context
    from market_agent.research_task import numeric_facts
    t=task();evidence=valuation_evidence(t)
    assert evidence.content_hash==content_hash(evidence.structured_data)
    saved=t.model_copy(update={'sources':[*t.sources,ResearchSource(
        identity='VERIFIED_FACT',evidence=evidence,freshness='CURRENT')]})
    facts,selected,manifest=build_context(saved,supplemental_allowed=True)
    row=next(s for s in facts['sources'] if s['evidence']['evidence_id']==evidence.evidence_id)
    assert row['evidence']['structured_data']['calculation']=='task_valuation_v1'
    assert row['evidence']['structured_data']['metrics']['ttm_revenue']['value']=='1000'
    allowed=numeric_facts(evidence.model_dump(mode='json'))
    assert numeric_facts('Historical TTM revenue 1000 USD')<=allowed
    assert not numeric_facts('Historical TTM revenue 9999 USD')<=allowed
    assert evidence.evidence_id in {s.evidence.evidence_id for s in selected}
    assert manifest['context_bytes']<=manifest['max_context_bytes']


def test_valuation_api_historical_revision_never_borrows_latest_inputs(tmp_path):
    app=create_app(db_path=tmp_path/'revision.db',market_fixture='evals/fixtures/market_eod.json')
    first=task();second=changed_source(first,0,lambda d:d.update(price='25')).model_copy(update={'revision':2})
    from evals.v09_intelligence import qualified_price
    sources=list(second.sources);sources[0]=sources[0].model_copy(update={
        'evidence':qualified_price(sources[0].evidence,[s.evidence for s in sources[1:]])})
    second=second.model_copy(update={'sources':sources})
    store=app.state.research_service.store
    with store._connect() as db:
        for t in (first,second):
            payload=t.model_dump(mode='json');raw=json.dumps(payload,ensure_ascii=False)
            db.execute('INSERT INTO research_task_turns VALUES(?,?,?,?)',
                (t.task_id,t.revision,content_hash(payload),raw))
        db.execute('INSERT INTO research_tasks VALUES(?,?,?,?,?,?)',
            (second.task_id,2,'COMPLETE',NOW.isoformat(),content_hash(payload),raw))
        original=list(db.execute('SELECT * FROM research_task_turns'))
    with TestClient(app) as client:
        path='/api/research/tasks/'+first.task_id+'/valuation'
        assert Decimal(client.get(path+'?revision=1').json()['metrics']['pe_ttm']['value'])==Decimal('5')
        assert Decimal(client.get(path).json()['metrics']['pe_ttm']['value'])==Decimal('10')
        assert client.get(path+'?revision=0').status_code==422
    with store._connect() as db:
        assert list(db.execute('SELECT * FROM research_task_turns'))==original
        assert db.execute('SELECT COUNT(*) FROM query_runs').fetchone()[0]==0


def test_bad_session_source_parent_and_malformed_quarter_fail_closed():
    bad=changed_source(task(),0,lambda d:d.update(session_date='2027-01-01'))
    assert valuation_report(bad).price_reference is None
    for transform in [lambda d:d.update(source_evidence_ids=['Emissing']),
                      lambda d:d.update(quarter=None),
                      lambda d:d['quarter'].update(metrics=[None])]:
        report=valuation_report(changed_source(task(),2,transform))
        assert report.metrics['ttm_revenue'].status=='UNKNOWN'
    bad=changed_source(task(),1,lambda d:d.update(symbol='SMR'))
    assert valuation_report(bad).shares_reference is None


def test_read_only_api_uses_frozen_bytes_hash_and_never_updates_saved_task(tmp_path):
    app=create_app(db_path=tmp_path/'projection.db',market_fixture='evals/fixtures/market_eod.json')
    t=task();payload=t.model_dump(mode='json')
    # Old DEEP payload omits mode: insert raw bytes directly, not a renamed model.
    raw=json.dumps(payload,ensure_ascii=False,separators=(',',':'))
    store=app.state.research_service.store
    with store._connect() as db:
        db.execute('INSERT INTO research_tasks VALUES(?,?,?,?,?,?)',
            (t.task_id,1,'COMPLETE',NOW.isoformat(),content_hash(payload),raw))
    with TestClient(app) as client:
        response=client.get('/api/research/tasks/'+t.task_id+'/valuation')
        assert response.status_code==200
        assert response.json()['metrics']['market_cap_reference']['value']=='1250.00'
        assert response.json()['basis']=='FROZEN_TASK_INPUTS_NO_REFRESH'
        assert client.get('/api/research/tasks/'+t.task_id+'/valuation').json()==response.json()
    with store._connect() as db:
        assert db.execute('SELECT payload_hash,payload FROM research_tasks').fetchone()==(content_hash(payload),raw)
        assert db.execute('SELECT COUNT(*) FROM query_runs').fetchone()[0]==0


def test_shares_normalizer_excludes_weighted_average_ambiguous_and_future_records():
    filings=[{'accession_number':'a','filed_at':AVAILABLE}]
    row={'accn':'a','form':'10-Q','end':'2026-09-30','val':100}
    raw={'facts':{'us-gaap':{'CommonStockSharesOutstanding':{'units':{'shares':[row]}},
                              'WeightedAverageNumberOfDilutedSharesOutstanding':{'units':{'shares':[{**row,'val':10000}]}}}}}
    result=common_shares(raw,filings,NOW)
    assert result['value']=='100' and result['unit']=='shares' and result['scope']=='ENTIRE_FILING_ENTITY'
    raw['facts']['us-gaap']['CommonStockSharesOutstanding']['units']['shares'].append({**row,'val':110})
    assert common_shares(raw,filings,NOW) is None
    assert common_shares(raw,[{'accession_number':'a','filed_at':NOW.replace(year=2027)}],NOW) is None
