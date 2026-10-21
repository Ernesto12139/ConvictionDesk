"""Owned synthetic OHLCV, worked rational oracle and qualification counterexamples."""
from copy import deepcopy
from datetime import datetime,timezone
from decimal import Decimal,localcontext
from fractions import Fraction
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from market_agent.technical_zones import TechnicalRequest,technical_zones,close_time
from market_agent.tdb import content_hash
from test_portfolio_whatif import example

FIXTURE=Path(__file__).resolve().parents[1]/'src/market_agent/demo_assets/technical-example-v1.json'

def payload(symbol='AAPL'):
    data=json.loads(FIXTURE.read_text());data['symbol']=symbol
    return {'revision':1,'series':data}

@pytest.mark.parametrize('symbol',['AAPL','JPM','SMR'])
def test_generic_daily_zones_and_receipt_never_change_saved_action(symbol):
    task,_=example(symbol);old=task.model_dump_json()
    result=technical_zones(task,TechnicalRequest.model_validate(payload(symbol)))
    assert result['status']=='AVAILABLE_SYNTHETIC'
    # Worked linear sequence closes101..120 => (101+120)/2=110.5.
    # Each true range is4; last10 extrema are109,122 => half ATR band=2.
    assert Decimal(result['sma20'])==Decimal('110.5') and Decimal(result['atr14'])==4
    assert list(map(Decimal,result['zones']['support']))==[109,111]
    assert list(map(Decimal,result['zones']['resistance']))==[120,122]
    assert result['original_action']=='HOLD' and not result['action_changed']
    assert result['live_data_status']=='LIVE_DATA_NOT_VERIFIED'
    assert result['content_hash']==content_hash({k:v for k,v in result.items() if k!='content_hash'})
    assert task.model_dump_json()==old
    assert result['valid_until']=='2026-10-19T20:00:00+00:00'

def test_gap_true_range_rational_oracle_and_low_ambient_precision():
    task,_=example();data=payload();data['series']['bars'][-1]['high']='132'
    with localcontext() as ctx:
        ctx.prec=3
        result=technical_zones(task,TechnicalRequest.model_validate(data))
    assert abs(Fraction(result['atr14'])-Fraction(33,7))<Fraction(1,10**50)
    exact=Fraction(132)-Fraction(33,14)
    rounded=Fraction(result['zones']['resistance'][0])
    assert rounded<=exact<rounded+Fraction(1,10**10)

@pytest.mark.parametrize('field,value,reason',[
    ('identity','REAL','REAL_SOURCE_RIGHTS_ADAPTER_NOT_ADMITTED'),
    ('identity','USER_ASSERTION','REAL_SOURCE_RIGHTS_ADAPTER_NOT_ADMITTED'),
    ('usage_rights','DISPLAY_ONLY','REAL_SOURCE_RIGHTS_ADAPTER_NOT_ADMITTED'),
    ('session','EXTENDED','REGULAR_SESSION_REQUIRED'),
    ('adjustment','UNKNOWN','ADJUSTMENT_LINEAGE_REQUIRED'),
    ('adjustment_lineage','','ADJUSTMENT_LINEAGE_REQUIRED'),
    ('split_status','CHANGE_DETECTED','SPLIT_BASIS_UNRECONCILED'),
    ('split_status','UNKNOWN','SPLIT_BASIS_UNRECONCILED'),
    ('halt_status','HALTED','HALT_STATUS_UNQUALIFIED'),
    ('halt_status','UNKNOWN','HALT_STATUS_UNQUALIFIED'),
    ('share_class','MULTI_CLASS','SHARE_CLASS_UNRECONCILED'),
    ('retrieved_at','2026-10-18T12:00:00Z','POST_CUTOFF_VINTAGE'),
    ('adjustment_as_of','2026-10-18T12:00:00Z','POST_CUTOFF_VINTAGE'),
])
def test_unqualified_sources_have_no_numeric_output(field,value,reason):
    task,_=example();data=payload();data['series'][field]=value
    result=technical_zones(task,TechnicalRequest.model_validate(data))
    assert result['status']=='UNAVAILABLE' and result['reason']==reason and result['zones'] is None
    assert not any(k in result for k in ('sma20','atr14','last_close'))

@pytest.mark.parametrize('mutation,reason',[
    ('short','AT_LEAST_21_COMPLETED_BARS_REQUIRED'),('duplicate','DUPLICATE_OR_UNSORTED_SESSIONS'),
    ('gap','MISSING_TRADING_SESSION'),('stale','STALE_OR_FUTURE_SERIES'),
    ('incomplete','INCOMPLETE_OR_POST_CUTOFF_BAR'),('zero_volume','HALTED_OR_ZERO_VOLUME_BAR'),
])
def test_session_completeness_and_staleness_refuse(mutation,reason):
    task,_=example();data=payload();bars=data['series']['bars']
    if mutation=='short':bars.pop()
    elif mutation=='duplicate':bars.append(deepcopy(bars[-1]))
    elif mutation=='gap':
        bars.pop(10);bars.insert(0,{**bars[0],'session_date':'2026-09-17','available_at':'2026-09-17T20:00:00Z'})
    elif mutation=='stale':
        bars.pop();bars.insert(0,{**bars[0],'session_date':'2026-09-17','available_at':'2026-09-17T20:00:00Z'})
    elif mutation=='incomplete':bars[-1]['available_at']='2026-10-16T19:59:59Z'
    elif mutation=='zero_volume':bars[-1]['volume']=0
    result=technical_zones(task,TechnicalRequest.model_validate(data))
    assert result['reason']==reason and result['zones'] is None

@pytest.mark.parametrize('field,value',[('high','90'),('close','NaN'),('open',True),('volume',1.5),('low',0)])
def test_ohlcv_numeric_integrity_is_typed(field,value):
    data=payload();data['series']['bars'][0][field]=value
    with pytest.raises(ValidationError):TechnicalRequest.model_validate(data)

def test_calendar_regular_dst_early_close_and_holidays():
    from datetime import date
    assert close_time(date(2026,10,16)).hour==20
    assert close_time(date(2026,11,27)).hour==18
    assert close_time(date(2026,12,24)).hour==18
    assert close_time(date(2026,9,7)) is None
    with pytest.raises(ValueError,match='YEAR_NOT_SUPPORTED'):close_time(date(2029,1,2))

def test_adjusted_basis_keeps_explicit_lineage_but_known_task_class_conflict_refuses():
    from test_valuation import changed_source
    task,_=example();data=payload();data['series']['adjustment']='SPLIT_ADJUSTED'
    result=technical_zones(task,TechnicalRequest.model_validate(data))
    assert result['series']['adjustment']=='SPLIT_ADJUSTED'
    task=changed_source(task,1,lambda d:d.update(listed_common_symbols=['AAPL','AAPL.B']))
    assert technical_zones(task,TechnicalRequest.model_validate(data))['reason']=='SAVED_EVIDENCE_CAPITAL_CONFLICT'


def test_adjustment_known_after_retrieval_is_not_admitted():
    task,_=example();data=payload()
    data['series']['retrieved_at']='2026-10-17T10:00:00Z'
    data['series']['adjustment_as_of']='2026-10-17T11:00:00Z'
    assert technical_zones(task,TechnicalRequest.model_validate(data))['reason']=='ADJUSTMENT_AFTER_RETRIEVAL'


def test_tiny_range_that_collapses_after_outward_rounding_refuses():
    task,_=example();data=payload()
    for bar in data['series']['bars']:
        bar.update(open='1',high='1',low='1',close='1')
    data['series']['bars'][-1]['high']='1.0000000001'
    assert technical_zones(task,TechnicalRequest.model_validate(data))['reason']=='ROUNDED_RANGE_NOT_SEPARABLE'

def test_real_http_no_provider_or_history_writes(tmp_path,monkeypatch):
    from evals.closure_journey import create_app
    monkeypatch.setenv('MIA_ISOLATED_ROOT',str(tmp_path));monkeypatch.setenv('MIA_DB_PATH',str(tmp_path/'import.sqlite3'))
    app=create_app();service=app.state.research_service
    task,snapshot=example();service.store.add_snapshot(snapshot);service.tasks.save(task,new=True)
    with TestClient(app) as client:
        path='/api/research/tasks/'+task.task_id
        before=client.get(path).json();calls=len(app.state.fixture_model.requests)
        example_json=client.get(path+'/technical-example?revision=1').raise_for_status().json()
        response=client.post(path+'/technical-zones',json=example_json).raise_for_status().json()
        assert response['status']=='AVAILABLE_SYNTHETIC' and response['persisted'] is False
        assert client.get(path).json()==before and len(app.state.fixture_model.requests)==calls
        assert service.store.get_snapshot().content_hash==snapshot.content_hash


@pytest.mark.parametrize('symbol',['AAPL','JPM','SMR'])
@pytest.mark.parametrize('factor',['10','0.1','1.2','0.8'])
@pytest.mark.parametrize('adjustment',['UNADJUSTED','SPLIT_ADJUSTED'])
def test_asserted_no_split_cannot_bypass_price_discontinuity(symbol,factor,adjustment):
    task,_=example(symbol);data=payload(symbol);data['series']['adjustment']=adjustment
    # Independent arithmetic: piecewise price scale simulates raw or falsely
    # adjusted split/reverse-split sequences. No production ticker special case.
    for bar in data['series']['bars'][10:]:
        for field in ('open','high','low','close'):
            bar[field]=str(Decimal(bar[field])*Decimal(factor))
    before=task.model_dump_json()
    result=technical_zones(task,TechnicalRequest.model_validate(data))
    assert result['reason']=='PRICE_DISCONTINUITY_REQUIRES_RECONCILIATION'
    assert result['zones'] is None and 'sma20' not in result
    assert task.model_dump_json()==before


def test_news_gap_is_review_not_false_split_diagnosis():
    task,_=example();data=payload()
    data['series']['bars'][-1].update(open='150',high='152',low='148',close='151')
    result=technical_zones(task,TechnicalRequest.model_validate(data))
    assert result['reason']=='PRICE_DISCONTINUITY_REQUIRES_RECONCILIATION'
    assert result['status']=='UNAVAILABLE'  # does not claim a corporate action


def test_intraday_jump_cannot_hide_behind_continuous_open():
    task,_=example();data=payload()
    data['series']['bars'][-1].update(open='120',high='152',low='118',close='150')
    assert technical_zones(task,TechnicalRequest.model_validate(data))['reason']=='PRICE_DISCONTINUITY_REQUIRES_RECONCILIATION'
