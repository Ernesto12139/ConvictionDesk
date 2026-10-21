"""Synthetic economic archetypes and independent rational/integer-cent oracles."""
from decimal import Decimal, localcontext
from fractions import Fraction

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from market_agent.action_decision import ActionDecision
from market_agent.portfolio_entry import PortfolioEntry
from market_agent.portfolio_whatif import WhatIfRequest, WhatIfUnavailable, simulate
from market_agent.tdb import content_hash
from test_valuation import NOW, task as valuation_task, changed_source


def example(symbol='AAPL', held=True):
    snapshot = PortfolioEntry(accounts=[{'account_id':'one','name':'Synthetic cash account','cash':'1000'},
        {'account_id':'two','name':'Other account','cash':'9000'}], positions=[
        *([{'position_id':'focus','account_id':'one','symbol':symbol,'quantity':'10','avg_cost':'80'}] if held else []),
        {'position_id':'other','account_id':'one','symbol':'OTHER','quantity':'4','avg_cost':'40'},
        {'position_id':'separate','account_id':'two','symbol':symbol,'quantity':'99','avg_cost':'2'}]).snapshot(NOW)
    original = valuation_task(symbol, loss=symbol=='SMR')
    action = ActionDecision(symbol=symbol, decision_action='HOLD' if held else 'ADD', decision_status='PROVISIONAL',
        evidence_strength='LIMITED', thesis_delta='Synthetic test only', key_drivers=[], counterevidence=[],
        change_conditions=[], source_ids=[], research_cutoff=NOW.isoformat(), model_profile=None,
        actual_model='scripted', trace_id=original.task_id, position_status='HELD' if held else 'NOT_HELD')
    return original.model_copy(update={'snapshot_id':snapshot.snapshot_id, 'snapshot_hash':snapshot.content_hash,
        'account_id':None, 'action_decision':action}), snapshot


def request(symbol='AAPL', **changes):
    values = dict(revision=1,account_id='one',symbol=symbol,side='BUY',shares='2',fill_price='100',fee='1',
        scenario_date=NOW.date(),unchanged_share_basis_attested=True,other_prices={'OTHER':'50'})
    values.update(changes)
    return WhatIfRequest.model_validate(values)


@pytest.mark.parametrize('symbol,archetype', [('AAPL','profitable consumer hardware'),
    ('JPM','financial institution'), ('SMR','pre-commercial capital-intensive')])
@pytest.mark.parametrize('held', [True,False])
def test_same_saved_action_journey_for_three_economic_types_held_and_unheld(symbol,archetype,held):
    task,snapshot=example(symbol,held); original=(task.model_dump_json(),snapshot.model_dump_json())
    value=simulate(task,snapshot,request(symbol))
    assert value['before']['shares']==('10' if held else '0')
    assert value['after']['shares']==('12' if held else '2')
    # Worked integer-cent oracle: 100000 - 2*10000 - 100 = 79900 cents.
    assert Decimal(value['after']['cash'])==Decimal(79900)/100
    assert Decimal(value['after']['nav'])==Decimal(2199 if held else 1199)
    ratio=Fraction(value['after']['focus_market_value'])/Fraction(value['after']['nav'])
    assert abs(Fraction(value['after']['focus_weight'])-ratio)<Fraction(1,10**70)
    assert value['focus_cost_after']==('1001' if held else '201')
    assert value['modeled_unrealized_pnl_after']==('199' if held else '-1')
    assert value['identity']=='USER_ASSERTION' and value['actual_quote'] is None
    assert value['actual_fill'] is False and value['orders_created']==0 and value['persisted'] is False
    assert content_hash({k:v for k,v in value.items() if k!='content_hash'})==value['content_hash']
    assert original==(task.model_dump_json(),snapshot.model_dump_json())


def test_sell_average_cost_fees_and_complete_exit():
    task,snapshot=example()
    value=simulate(task,snapshot,request(side='SELL',shares='3'))
    assert value['after']['cash']=='1299' and value['after']['shares']=='7'
    assert value['focus_cost_after']=='560' and value['modeled_realized_pnl']=='59'
    assert value['after']['nav']=='2199'
    exit_value=simulate(task,snapshot,request(side='SELL',shares='10'))
    assert exit_value['after']['shares']=='0' and exit_value['focus_cost_after']=='0'
    assert exit_value['after']['focus_weight']=='0'


def test_missing_marks_remain_unknown_and_do_not_prevent_known_cash_delta():
    task,snapshot=example()
    value=simulate(task,snapshot,request(other_prices={}))
    assert value['after']['cash']=='799'
    for state in ('before','after'):
        assert value[state]['missing_price_symbols']==['OTHER']
        assert all(value[state][key] is None for key in ('nav','equity','focus_weight','largest_weight','hhi_nav'))


@pytest.mark.parametrize('changes,code', [({'shares':'11','side':'SELL'},'OVERSELL_NOT_ALLOWED'),
    ({'shares':'10'},'INSUFFICIENT_CASH_INCLUDING_FEES'),
    ({'account_id':'absent'},'ACCOUNT_OUTSIDE_SAVED_SCOPE'),
    ({'symbol':'DIFFERENT'},'TASK_INSTRUMENT_OR_REVISION_MISMATCH'),
    ({'revision':2},'TASK_INSTRUMENT_OR_REVISION_MISMATCH'),
    ({'unchanged_share_basis_attested':False},'SHARE_BASIS_CONTINUITY_ATTESTATION_REQUIRED'),
    ({'other_prices':{'UNKNOWN':'1'}},'MARKS_MUST_MATCH_OTHER_ACCOUNT_HOLDINGS'),
    ({'shares':'0'},'POSITIVE_SHARES_AND_PRICES_REQUIRED'),
    ({'scenario_date':'2026-10-01'},'SCENARIO_PREDATES_SNAPSHOT')])
def test_refusals(changes,code):
    task,snapshot=example()
    with pytest.raises(WhatIfUnavailable,match=code):simulate(task,snapshot,request(**changes))


@pytest.mark.parametrize('changes', [{'currency':'AUD'},{'fill_price':1.1},{'shares':True},
    {'fee':'NaN'},{'fill_price':'Infinity'},{'fee':'-1'},{'shares':'1e100'},
    {'other_prices':{'OTHER':2.2}}, {'other_prices':{'OTHER':'NaN'}}])
def test_non_usd_inexact_nonfinite_and_unbounded_inputs_rejected(changes):
    with pytest.raises(ValidationError):request(**changes)


@pytest.mark.parametrize('data', [{'listed_common_symbols':['AAPL','AAPL.B']},
    {'issuer_listing_identity':{'tickers':['AAPL','AAPL.B']}},
    {'capital_basis_check':{'split_status':'CHANGE_DETECTED'}},
    {'capital_basis_check':{'share_class_alignment':'MISMATCH'}}])
def test_known_multiclass_or_abnormal_capital_refuses_even_with_attestation(data):
    task,snapshot=example()
    task=changed_source(task,1,lambda d:d.update(data))
    with pytest.raises(WhatIfUnavailable,match='KNOWN_CAPITAL_OR_SHARE_CLASS_CONFLICT'):
        simulate(task,snapshot,request())


def test_low_ambient_precision_does_not_round_cash_or_position_cost():
    task,snapshot=example()
    with localcontext() as ctx:
        ctx.prec=3
        value=simulate(task,snapshot,request(shares='0.123456789',fill_price='100.123456789'))
    expected=Fraction(1000)-Fraction('0.123456789')*Fraction('100.123456789')-1
    assert Fraction(value['after']['cash'])==expected


def test_large_old_holding_revaluation_is_not_profit_created_by_buying():
    task,snapshot=example()
    result=simulate(task,snapshot,request(shares='0.5',fill_price='1000',reference_mark='50'))
    assert Decimal(result['before']['nav'])==11200 and Decimal(result['after']['nav'])==11199
    assert result['baseline_before']['nav']=='1700'
    assert result['market_repricing_delta']=='9500' and result['trade_nav_delta']=='-1'
    assert result['total_nav_delta']=='9499'
    assert Decimal(result['modeled_unrealized_pnl_after'])==9199
    assert result['repricing_identity'].startswith('USER_ASSERTION')
    unknown=simulate(task,snapshot,request(shares='0.5',fill_price='1000'))
    assert unknown['market_repricing_delta'] is None and unknown['total_nav_delta'] is None
    assert unknown['trade_nav_delta']=='-1'


@pytest.mark.parametrize('field', ['shares','fill_price','fee','reference_mark'])
def test_scientific_notation_is_refused_with_clear_input_contract(field):
    with pytest.raises(ValidationError,match='ordinary decimal'):
        request(**{field:'1e1'})


def test_fractional_shares_are_exact_and_other_marks_refuse_exponent_notation():
    task,snapshot=example()
    result=simulate(task,snapshot,request(shares='0.125',fill_price='100',fee='0'))
    assert result['after']['shares']=='10.125' and result['after']['cash']=='987.500'
    assert result['trade_nav_delta']=='0'
    with pytest.raises(ValidationError,match='ordinary decimal'):
        request(other_prices={'OTHER':'5e1'})


def test_saved_account_scope_does_not_allow_another_account_and_unheld_cannot_sell():
    task,snapshot=example(held=False)
    with pytest.raises(WhatIfUnavailable,match='OVERSELL_NOT_ALLOWED'):
        simulate(task,snapshot,request(side='SELL',shares='1'))
    task=task.model_copy(update={'account_id':'one'})
    with pytest.raises(WhatIfUnavailable,match='ACCOUNT_OUTSIDE_SAVED_SCOPE'):
        simulate(task,snapshot,request(account_id='two'))


def test_zero_nav_is_not_zero_weight_and_fee_consumes_last_cash():
    task,snapshot=example()
    # Separate synthetic all-cash account with one fully sold position.
    from market_agent.domain import PortfolioContent, PortfolioSnapshot, portfolio_content_hash
    content=PortfolioContent.model_validate(snapshot.model_dump(exclude={'snapshot_id','content_hash'}))
    content=content.model_copy(update={'accounts':tuple(a.model_copy(update={'cash':Decimal(0)}) for a in content.accounts),
        'positions':tuple(p for p in content.positions if p.account_id=='one' and p.symbol=='AAPL')})
    snapshot=PortfolioSnapshot(**content.model_dump(),snapshot_id='zero-nav',content_hash=portfolio_content_hash(content))
    task=task.model_copy(update={'snapshot_id':snapshot.snapshot_id,'snapshot_hash':snapshot.content_hash})
    result=simulate(task,snapshot,request(side='SELL',shares='10',fee='1000',other_prices={}))
    assert result['after']['cash']=='0' and result['after']['nav']=='0'
    assert result['after']['focus_weight'] is None and result['after']['hhi_nav'] is None


def test_http_pinned_revision_and_snapshot_remain_immutable_without_model_calls(tmp_path,monkeypatch):
    from evals.closure_journey import create_app
    monkeypatch.setenv('MIA_ISOLATED_ROOT',str(tmp_path)); monkeypatch.setenv('MIA_DB_PATH',str(tmp_path/'isolated.sqlite3'))
    app=create_app();service=app.state.research_service
    task,snapshot=example()
    prose=task.sources[0].model_copy(update={'evidence':task.sources[0].evidence.model_copy(update={
        'evidence_id':'E-unstructured-news','evidence_type':'news','structured_data':None,
        'excerpt':'Synthetic prose without structured financial data'})})
    task=task.model_copy(update={'sources':[*task.sources,prose]})
    service.store.add_snapshot(snapshot);service.tasks.save(task,new=True)
    with TestClient(app) as client:
        path='/api/research/tasks/'+task.task_id
        old=client.get(path+'/revisions/1').json()
        calls=len(app.state.fixture_model.requests)
        response=client.post(path+'/what-if',json=request().model_dump(mode='json'))
        assert response.status_code==200,response.text
        assert response.json()['after']['cash']=='799'
        assert client.get(path+'/what-if?revision=1').json()['accounts'][0]['account_id']=='one'
        assert client.get(path+'/revisions/1').json()==old
        assert service.store.get_snapshot().model_dump_json()==snapshot.model_dump_json()
        assert len(app.state.fixture_model.requests)==calls
        refusal=client.post(path+'/what-if',json=request(side='SELL',shares='11').model_dump(mode='json'))
        assert refusal.status_code==422 and refusal.json()['detail']=='OVERSELL_NOT_ALLOWED'


def test_zero_quantities_and_deltas_are_plain_unsigned_decimals():
    # Owner-observed F5: an unheld account plus a reference mark printed -0E-10.
    task,snapshot=example(held=False)
    value=simulate(task,snapshot,request(fill_price='100.0000000000',reference_mark='150.0000000000',fee='0'))
    assert value['market_repricing_delta']=='0.0000000000'
    assert value['trade_nav_delta']=='0' and value['before']['shares']=='0'
    texts=[v for v in (value['market_repricing_delta'],value['trade_nav_delta'],value['total_nav_delta'],
        value['cash_delta'],value['modeled_unrealized_pnl_after'],*value['before'].values(),*value['after'].values())
        if isinstance(v,str)]
    assert not [v for v in texts if 'E' in v.upper() or v.startswith('-0') and Decimal(v)==0]
    assert Decimal(value['total_nav_delta'])==0
