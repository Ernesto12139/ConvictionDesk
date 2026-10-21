"""Independent arithmetic targets and incompatible-input counterexamples, offline."""
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from market_agent.dated_valuation import PriceEvidence, UserPriceRequest, dated_valuation, derivation_allowed
from market_agent.main import create_app
from market_agent.tdb import content_hash
from test_valuation import NOW, task, changed_source


def company(symbol='SOFT', loss=False):
    t=changed_source(task(symbol, loss=loss), 1, lambda d:d['metrics'][0].update(accession_number='0000000001-26-000001'))
    return changed_source(t,1,lambda d:d.update(issuer_listing_identity={
        'tickers':[symbol],'observed_at':'2026-10-10T12:00:00+00:00'}))


def request(**changes):
    payload={'revision':1,'symbol':'SOFT','price':'12.50','currency':'USD','price_date':'2026-10-16',
        'session':'COMPLETED_CLOSE','kind':'USER_SUPPLIED_OBSERVATION','source_basis':'OWN_OBSERVATION',
        'source_note':'Own synthetic observation, no commercial feed', 'local_calculation_rights_attested':True,
        'assumptions':{'unchanged_since_share_date':True,'entire_common_equity_at_input_class_price':True,
            'consolidated_income_available_to_common':True}}
    payload.update(changes)
    return UserPriceRequest.model_validate(payload)


def test_conditional_equity_value_and_company_profit_multiple_not_cross_period_eps():
    original=company();before=original.model_dump_json()
    result=dated_valuation(original,request(),NOW)
    report=result['report'];metrics=report['metrics']
    assert report['qualification_status']=='CONDITIONAL_REFERENCE'
    # Integer cents oracle:1250c *100shares /100 =1250USD, then /100 profit =12.5.
    assert Decimal(metrics['reference_equity_value']['value'])==Decimal(1250*100)/100
    assert Decimal(metrics['reference_ps']['value'])==Decimal('1.25')
    assert Decimal(metrics['reference_pe']['value'])==Decimal('12.5')
    assert Decimal(metrics['reference_pe']['value'])!=Decimal('5')  # Existing EPS-ratio fixture.
    assert report['shares_as_of']=='2026-09-30' and report['filing_available_at'].startswith('2026-10-10')
    assert report['price_as_of']=='2026-10-16' and report['research_cutoff']==NOW.isoformat()
    assert len(report['required_conditions'])==3
    assert report['action_applicability']['original_action_unchanged'] and not report['action_applicability']['sent_to_deep']
    assert result['persisted'] is False and result['external_calls']==0
    assert original.model_dump_json()==before
    for key in ('price_receipt','calculation_receipt'):
        receipt=result[key];assert receipt['content_hash']==content_hash(receipt['structured_data'])
    assert result['price_receipt']['provider']=='user'
    assert report['price_evidence']['trust_status']=='USER_ASSERTION'
    assert report['price_evidence']['source_id'] in report['source_ids']
    assert report['bound_task_projection_hash']==content_hash(original.model_dump(mode='json'))


def test_hypothetical_identity_cannot_be_promoted_to_an_observed_quote():
    result=dated_valuation(company(),request(kind='USER_SUPPLIED_SCENARIO',session='HYPOTHETICAL',
        source_basis='HYPOTHETICAL',source_note='My hypothetical price'),NOW)['report']
    assert result['qualification_status']=='ILLUSTRATIVE_SCENARIO'
    assert result['metrics']['reference_equity_value']['value']=='1250.00'
    assert result['price_evidence']['trust_status']=='USER_ASSERTION'
    assert result['context']=='DETACHED_LOCAL_WHAT_IF_NOT_ORIGINAL_RESEARCH_EVIDENCE'


def test_loss_early_commercial_zero_sales_is_not_meaningful_and_no_action_created():
    result=dated_valuation(company('ENERGY',True),request(symbol='ENERGY'),NOW)['report']
    assert result['metrics']['reference_pe']['status']=='NOT_MEANINGFUL'
    assert result['metrics']['reference_ps']['status']=='NOT_MEANINGFUL'
    assert result['metrics']['reference_pe']['value'] is None
    assert 'decision_action' not in result and result['action_applicability']['original_action_unchanged']


def test_consolidated_income_never_silently_becomes_common_share_earnings():
    assumptions=request().assumptions.model_copy(update={'consolidated_income_available_to_common':False})
    result=dated_valuation(company(),request(assumptions=assumptions),NOW)['report']
    assert result['metrics']['reference_ps']['value']=='1.25'
    assert result['metrics']['reference_pe']['value'] is None
    assert result['metrics']['reference_pe']['reason']=='COMMON_INCOME_ATTRIBUTION_NOT_ESTABLISHED'


@pytest.mark.parametrize('changes,reason',[
    ({'symbol':'OTHER'},'TASK_INSTRUMENT_OR_REVISION_MISMATCH'),
    ({'revision':2},'TASK_INSTRUMENT_OR_REVISION_MISMATCH'),
    ({'price_date':'2026-10-19'},'PRICE_AFTER_RESEARCH_CUTOFF_CREATE_NEW_CONTEXT'),
    ({'price_date':'2026-10-17'},'OBSERVATION_NON_WEEKDAY'),
    ({'price_date':'2026-09-07'},'OBSERVATION_PUBLISHED_MARKET_CLOSURE'),
    ({'price_date':'2026-09-29'},'SHARE_DATE_MUST_PRECEDE_PRICE_WITHIN_120_DAYS'),
    ({'price_date':'2026-10-09'},'SHARE_FILING_NOT_PUBLIC_BY_PRICE_DATE'),
    ({'source_basis':'THIRD_PARTY'},'SERVER_VERIFIED_PROVIDER_DERIVATION_RIGHTS_REQUIRED'),
    ({'source_note':'Copied Massive display-only price'},'DISPLAY_ONLY_PROVIDER_NOT_ADMITTED_FOR_DERIVATION'),
    ({'local_calculation_rights_attested':False},'LOCAL_DERIVATION_RIGHTS_NOT_ATTESTED'),
    ({'assumptions':{}},'EXPLICIT_CAPITAL_CONTINUITY_AND_CLASS_ASSUMPTIONS_REQUIRED'),
    ({'session':'HYPOTHETICAL'},'OBSERVATION_SOURCE_OR_SESSION_MISMATCH'),
])
def test_bad_price_time_identity_rights_or_missing_assumptions_has_no_values(changes,reason):
    report=dated_valuation(company(),request(**changes),NOW)['report']
    assert report['qualification_status']=='UNAVAILABLE' and reason in report['rejected_inputs']
    assert all(row['value'] is None for row in report['metrics'].values())


@pytest.mark.parametrize('value',[0,'-1','NaN','Infinity',True,1.1,'1e100','0.000000000001'])
def test_non_exact_unsafe_price_rejected(value):
    with pytest.raises(ValidationError):request(price=value)


def test_hypothetical_label_is_not_a_workaround_for_a_known_display_only_copy():
    r=request(kind='USER_SUPPLIED_SCENARIO',source_basis='HYPOTHETICAL',session='HYPOTHETICAL',
        source_note='Hypothesis copied from Massive personal display close')
    report=dated_valuation(company(),r,NOW)['report']
    assert report['qualification_status']=='UNAVAILABLE'
    assert 'DISPLAY_ONLY_PROVIDER_NOT_ADMITTED_FOR_DERIVATION' in report['rejected_inputs']


@pytest.mark.parametrize('day',['2026-04-03','2026-06-19','2026-07-03','2026-09-07'])
def test_weekday_exchange_closures_are_not_treated_as_observed_regular_sessions(day):
    report=dated_valuation(company(),request(price_date=day),NOW)['report']
    assert report['qualification_status']=='UNAVAILABLE'
    assert 'OBSERVATION_PUBLISHED_MARKET_CLOSURE' in report['rejected_inputs']


@pytest.mark.parametrize('key,value', [('currency','CAD'),('adjustment_basis','SPLIT_ADJUSTED'),
    ('session','POST_MARKET'),('provider_identity','licensed'),('usage_rights','SERVER_VERIFIED_DERIVATION')])
def test_client_cannot_add_unsupported_price_or_self_certify_licensed_adapter(key,value):
    with pytest.raises(ValidationError):request(**{key:value})


@pytest.mark.parametrize('change', ['weighted','scope','multi','conflict','tamper','missing'])
def test_class_scope_conflict_bad_source_cannot_be_overridden_by_user_assumptions(change):
    t=company()
    if change=='weighted':t=changed_source(t,1,lambda d:d['metrics'][0].update(concept='us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding'))
    elif change=='scope':t=changed_source(t,1,lambda d:d['metrics'][0].update(scope='CLASS_B_ONLY'))
    elif change=='multi':t=changed_source(t,1,lambda d:d.update(listed_common_symbols=['SOFT','SOFT.B']))
    elif change=='conflict':t=changed_source(t,0,lambda d:d['capital_basis_check'].update(split_status='CHANGE_DETECTED'))
    elif change=='missing':t=t.model_copy(update={'sources':[t.sources[0],*t.sources[2:]]})
    else:
        sources=list(t.sources);sources[1]=sources[1].model_copy(update={'evidence':sources[1].evidence.model_copy(update={'content_hash':'sha256:tampered'})})
        t=t.model_copy(update={'sources':sources})
    report=dated_valuation(t,request(),NOW)['report']
    assert report['qualification_status']=='UNAVAILABLE'
    assert all(row['value'] is None for row in report['metrics'].values())


def test_partial_ttm_keeps_cap_but_no_fabricated_multiple():
    t=company();t=t.model_copy(update={'sources':t.sources[:-1]})
    report=dated_valuation(t,request(),NOW)['report']
    assert report['metrics']['reference_equity_value']['value']=='1250.00'
    assert report['metrics']['reference_ps']['value'] is None
    assert report['metrics']['reference_pe']['value'] is None


@pytest.mark.parametrize('count',['100.5','1e30'])
def test_invalid_entity_total_share_count_is_not_used(count):
    t=changed_source(company(),1,lambda d:d['metrics'][0].update(value=count))
    report=dated_valuation(t,request(),NOW)['report']
    assert report['qualification_status']=='UNAVAILABLE'
    assert 'SEC_COMMON_SHARE_COUNT_INVALID' in report['rejected_inputs']


def test_sec_map_multiple_instruments_are_a_conflict_not_an_assumption_override():
    t=changed_source(company(),1,lambda d:d.update(issuer_listing_identity={
        'cik':'0000000001','tickers':['SOFT','SOFT.B'],'observed_at':NOW.isoformat(),
        'source_uri':'https://www.sec.gov/files/company_tickers.json'}))
    report=dated_valuation(t,request(),NOW)['report']
    assert report['qualification_status']=='UNAVAILABLE'
    assert 'MULTI_CLASS_OR_INSTRUMENT_SCOPE_UNRECONCILED' in report['rejected_inputs']


def test_sec_share_only_summary_preserves_filing_time_and_listing_without_new_requests(tmp_path):
    import httpx
    from market_agent.persistence import Store
    from market_agent.sec import SECEDGARProvider
    requests=[]
    def handler(req):
        requests.append(req.url.path)
        if req.url.path.endswith('company_tickers.json'):
            return httpx.Response(200,json={'0':{'ticker':'SOFT','cik_str':1},'1':{'ticker':'SOFT.B','cik_str':1}})
        if '/submissions/' in req.url.path:
            return httpx.Response(200,json={'filings':{'recent':{'accessionNumber':['0000000001-26-000001'],
                'form':['10-Q'],'acceptanceDateTime':['2026-10-10T12:00:00Z'],'primaryDocument':['test.htm']}}})
        if '/companyfacts/' in req.url.path:
            return httpx.Response(200,json={'facts':{'us-gaap':{'CommonStockSharesOutstanding':{'units':{'shares':[
                {'val':100,'end':'2026-09-30','form':'10-Q','accn':'0000000001-26-000001'}]}}}}})
        raise AssertionError('Unexpected SEC path')
    sec=SECEDGARProvider(Store(tmp_path/'sec-only.db'),'qa@example.org',
        client=httpx.Client(transport=httpx.MockTransport(handler)),clock=lambda:NOW)
    rows=sec.fundamentals('SOFT',NOW)
    assert len(requests)==3 and len(rows)==1
    assert rows[0].structured_data['share_facts'][0]['end']=='2026-09-30'
    assert rows[0].published_at.isoformat().startswith('2026-10-10T12:00:00')
    assert rows[0].structured_data['issuer_listing_identity']['tickers']==['SOFT','SOFT.B']


def test_provider_contract_blocks_personal_display_and_unknown_rights():
    p=PriceEvidence(provider_identity='future-provider',instrument_identity='SOFT',price='10',currency='USD',
        session='COMPLETED_CLOSE',effective_date='2026-10-16',retrieved_at=NOW,adjustment_basis='UNADJUSTED',
        usage_rights='DISPLAY_ONLY',source_id='Etest',trust_status='PROVIDER_REPORTED')
    assert not derivation_allowed(p)
    assert not derivation_allowed(p.model_copy(update={'usage_rights':'UNKNOWN'}))
    assert not derivation_allowed(p.model_copy(update={'usage_rights':'USER_ATTESTED_LOCAL_ONLY'}))
    assert derivation_allowed(p.model_copy(update={'usage_rights':'SERVER_VERIFIED_DERIVATION'}))
    assert p.external_model_use=='NOT_AUTHORIZED'


def test_actual_api_pinned_history_no_model_no_task_write_and_tamper_fails(tmp_path,monkeypatch):
    app=create_app(db_path=tmp_path/'dated.db',market_fixture='evals/fixtures/market_eod.json')
    service=app.state.research_service;first=company();second=company().model_copy(update={'revision':2})
    second=changed_source(second,1,lambda d:d['metrics'][0].update(value='200'))
    service.now=lambda:NOW+timedelta(days=1)
    def forbidden(*args,**kwargs):raise AssertionError('No research or model submission authorized')
    monkeypatch.setattr(service,'begin',forbidden);monkeypatch.setattr(service,'execute',forbidden)
    with service.store._connect() as db:
        for t in (first,second):
            payload=t.model_dump(mode='json');raw=json.dumps(payload,ensure_ascii=False)
            db.execute('INSERT INTO research_task_turns VALUES(?,?,?,?)',(t.task_id,t.revision,content_hash(payload),raw))
        db.execute('INSERT INTO research_tasks VALUES(?,?,?,?,?,?)',(second.task_id,2,'COMPLETE',NOW.isoformat(),content_hash(payload),raw))
        before=list(db.execute('SELECT * FROM research_task_turns'))
        current=list(db.execute('SELECT * FROM research_tasks'))
    with TestClient(app) as client:
        url='/api/research/tasks/'+first.task_id+'/dated-valuation'
        old=client.post(url,json=request().model_dump(mode='json'))
        assert old.status_code==200
        assert old.json()['report']['metrics']['reference_equity_value']['value']=='1250.00'
        latest=client.post(url,json=request(revision=2).model_dump(mode='json'))
        assert latest.json()['report']['metrics']['reference_equity_value']['value']=='2500.00'
        assert client.post(url,json=request(revision=9).model_dump(mode='json')).status_code==404
        assert client.post(url,json={**request().model_dump(mode='json'),'price':2.5}).status_code==422
        assert client.get('/api/research/tasks/'+first.task_id).json()['revision']==2
        with service.store._connect() as db:
            assert list(db.execute('SELECT * FROM research_task_turns'))==before
            assert list(db.execute('SELECT * FROM research_tasks'))==current
            assert db.execute('SELECT COUNT(*) FROM query_runs').fetchone()[0]==0
            db.execute("UPDATE research_task_turns SET payload_hash='sha256:wrong' WHERE revision=1")
        assert client.post(url,json=request().model_dump(mode='json')).status_code==409


def test_actual_frozen_prior_version_bytes_survive_new_evaluation_endpoint(tmp_path):
    frozen=json.loads(Path('evals/fixtures/research_agent/pre-v05-research-task.json').read_text(encoding='utf-8'))
    raw,fingerprint=frozen['payload'],frozen['payload_hash'];payload=json.loads(raw)
    app=create_app(db_path=tmp_path/'actual-prior.db',market_fixture='evals/fixtures/market_eod.json')
    service=app.state.research_service
    with service.store._connect() as db:
        db.execute('INSERT INTO research_tasks VALUES(?,?,?,?,?,?)',(payload['task_id'],payload['revision'],
            payload['status'],payload['updated_at'],fingerprint,raw))
        db.execute('INSERT INTO research_task_turns VALUES(?,?,?,?)',(payload['task_id'],payload['revision'],fingerprint,raw))
    with TestClient(app) as client:
        response=client.post('/api/research/tasks/'+payload['task_id']+'/dated-valuation',
            json=request(symbol=payload['focus_symbol'],revision=payload['revision']).model_dump(mode='json'))
        assert response.status_code==200
        assert response.json()['report']['qualification_status']=='UNAVAILABLE'
        assert response.json()['persisted'] is False
    with service.store._connect() as db:
        assert db.execute('SELECT payload_hash,payload FROM research_tasks').fetchone()==(fingerprint,raw)
        assert db.execute('SELECT payload_hash,payload FROM research_task_turns').fetchone()==(fingerprint,raw)
        assert db.execute('SELECT COUNT(*) FROM query_runs').fetchone()[0]==0
