"""User-value deltas with integer oracles and actual/scenario negative controls."""
from decimal import Decimal
import pytest
from pydantic import ValidationError

from market_agent.dated_valuation import dated_valuation
from market_agent.tdb import content_hash
from test_dated_valuation import NOW, company, request
from test_valuation import changed_source


def test_legacy_listing_is_scenario_even_if_user_ticks_all_assumptions():
    old=changed_source(company(),1,lambda d:d.pop('issuer_listing_identity'))
    before=old.model_dump_json()
    report=dated_valuation(old,request(),NOW)['report']
    assert report['qualification_status']=='ILLUSTRATIVE_SCENARIO'
    assert report['qualification_notes']==['MISSING_POINT_IN_TIME_SINGLE_LISTING_IDENTITY']
    assert report['metrics']['reference_equity_value']['value']=='1250.00'
    assert report['metrics']['reference_equity_value']['reason']=='ILLUSTRATIVE_SCENARIO'
    assert report['price_evidence']['trust_status']=='USER_ASSERTION'
    assert old.model_dump_json()==before


@pytest.mark.parametrize('day,status',[('2026-07-08','CONDITIONAL_REFERENCE'),
    ('2026-07-07','ILLUSTRATIVE_SCENARIO'),('2026-06-19','ILLUSTRATIVE_SCENARIO'),
    ('2026-06-18','UNAVAILABLE')])
def test_100_day_reference_limit_and_120_day_scenario_limit_are_distinct(day,status):
    old=changed_source(company(),1,lambda d:d['metrics'][0].update(end=day))
    report=dated_valuation(old,request(),NOW)['report']
    assert report['qualification_status']==status
    if status=='ILLUSTRATIVE_SCENARIO':
        assert 'SHARES_OLDER_THAN_100_DAYS_SCENARIO_ONLY' in report['qualification_notes']
        assert report['metrics']['reference_equity_value']['value']=='1250.00'
    if status=='UNAVAILABLE':
        assert report['metrics']['reference_equity_value']['value'] is None


@pytest.mark.parametrize('observed',['2026-10-17T00:00:00+00:00','2026-10-18T00:00:00+00:00','bad'])
def test_later_or_unknown_listing_identity_never_promotes_historical_price(observed):
    t=changed_source(company(),1,lambda d:d['issuer_listing_identity'].update(observed_at=observed))
    report=dated_valuation(t,request(),NOW)['report']
    assert report['qualification_status']=='ILLUSTRATIVE_SCENARIO'
    assert report['listing_identity_source_ids']==[]


@pytest.mark.parametrize('price,delta,pct',[('10','-250','-20'),('12.50','0','0'),('15','250','20')])
def test_same_basis_price_difference_exact_oracle_and_no_market_or_action_claim(price,delta,pct):
    task=company();before=task.model_dump_json()
    result=dated_valuation(task,request(price=price,comparison_price='12.50'),NOW)
    report=result['report'];difference=report['scenario_difference']
    assert Decimal(difference['metrics']['reference_equity_value']['delta'])==Decimal(delta)
    assert Decimal(difference['price_delta_percent'])==Decimal(pct)
    assert Decimal(difference['metrics']['reference_ps']['delta'])==Decimal(delta)/1000
    assert Decimal(difference['metrics']['reference_pe']['delta'])==Decimal(delta)/100
    assert difference['comparison_identity']=='USER_DEFINED_SCENARIO_NOT_MARKET_OBSERVATION'
    assert difference['status']=='ILLUSTRATIVE_SCENARIO'
    assert 'OPERATING_THESIS' in difference['unchanged'] and 'ORIGINAL_ACTION' in difference['unchanged']
    assert task.model_dump_json()==before and result['persisted'] is False and result['external_calls']==0
    assert result['calculation_receipt']['content_hash']==content_hash(report)


def test_loss_and_unattributed_income_have_no_comparison_profit_multiple():
    loss=dated_valuation(company(loss=True),request(comparison_price='10'),NOW)['report']['scenario_difference']
    for key in ('reference_pe','reference_ps'):
        assert loss['metrics'][key]['delta'] is None
        assert loss['metrics'][key]['status']=='NOT_MEANINGFUL'
    assumptions=request().assumptions.model_copy(update={'consolidated_income_available_to_common':False})
    report=dated_valuation(company(),request(comparison_price='10',assumptions=assumptions),NOW)['report']
    assert report['scenario_difference']['metrics']['reference_pe']['comparison_value'] is None
    assert report['scenario_difference']['metrics']['reference_ps']['comparison_value']=='1'


@pytest.mark.parametrize('conflict',['multi','split'])
def test_scenario_comparison_never_overrides_known_conflicts(conflict):
    def change(data):
        if conflict=='multi':data['listed_common_symbols']=['SOFT','SOFT.B']
        else:data['capital_basis_check']={'split_status':'CHANGE_DETECTED'}
    report=dated_valuation(changed_source(company(),1,change),request(comparison_price='10'),NOW)['report']
    assert report['qualification_status']=='UNAVAILABLE'
    assert report['scenario_difference']['status']=='UNAVAILABLE'
    assert report['scenario_difference']['metrics']=={}


@pytest.mark.parametrize('price',[True,0,'0','NaN','Infinity','-1',1.2,'1e99','0.000000000001'])
def test_comparison_price_uses_the_same_exact_finite_bounds(price):
    with pytest.raises(ValidationError):request(comparison_price=price)
