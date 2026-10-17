from datetime import datetime, timezone

from market_agent.financial_series import quarterly_series, valuation_reference

NOW = datetime(2026,10,17,tzinfo=timezone.utc)
FILINGS = [{'accession_number':'known','filed_at':datetime(2026,8,5,tzinfo=timezone.utc)}]


def facts(tag, rows, unit='USD'):
    return {tag:{'units':{unit:[{'accn':'known','form':'10-Q',**row} for row in rows]}}}


def test_quarter_and_ytd_do_not_collapse_and_cash_flow_is_derived_with_lineage():
    gaap = facts('Revenues',[{'start':'2026-04-01','end':'2026-06-30','val':130},
        {'start':'2026-01-01','end':'2026-06-30','val':230}])
    gaap.update(facts('NetCashProvidedByUsedInOperatingActivities',[
        {'start':'2026-01-01','end':'2026-03-31','val':20},
        {'start':'2026-01-01','end':'2026-06-30','val':55}]))
    gaap.update(facts('PaymentsToAcquirePropertyPlantAndEquipment',[
        {'start':'2026-01-01','end':'2026-03-31','val':3},
        {'start':'2026-01-01','end':'2026-06-30','val':8}]))
    series = quarterly_series({'facts':{'us-gaap':gaap}},FILINGS,NOW)
    q2 = {row['metric']:row for row in series[0]['metrics']}
    assert q2['revenue']['value']=='130' and q2['revenue']['basis']=='REPORTED'
    assert q2['operating_cash_flow']['value']=='35'
    assert q2['free_cash_flow']['value']=='30'
    assert len(q2['operating_cash_flow']['inputs'])==2


def test_eps_is_not_derived_by_subtracting_ytd_and_future_filings_are_excluded():
    raw = {'facts':{'us-gaap':facts('EarningsPerShareDiluted',[
        {'start':'2026-01-01','end':'2026-03-31','val':'0.3'},
        {'start':'2026-01-01','end':'2026-06-30','val':'0.8'}],'USD/shares')}}
    assert len(quarterly_series(raw,FILINGS,NOW))==1
    assert quarterly_series(raw,[{'accession_number':'known','filed_at':NOW.replace(year=2027)}],NOW)==[]


def test_unreported_quarter_is_not_filled_with_zero():
    raw = {'facts':{'us-gaap':facts('Revenues',[{'start':'2026-01-01','end':'2026-06-30','val':230}])}}
    assert quarterly_series(raw,FILINGS,NOW)==[]


def test_loss_making_and_missing_valuation_remain_not_meaningful_or_unknown():
    assert valuation_reference(price='10',ttm_eps='-1')['pe_status']=='NOT_MEANINGFUL'
    assert valuation_reference(price='10')['pe'] is None
    assert valuation_reference(market_cap='100',cash='20')['enterprise_value'] is None
    assert valuation_reference(price='10',ttm_eps='2',market_cap='100',debt='5',cash='20') == {
        'pe':'5','enterprise_value':'85','pe_status':'AVAILABLE',
        'basis':'CALLER_MUST_SUPPLY_QUALIFIED_SAME_CURRENCY_INPUTS'}


def test_later_q1_restatement_does_not_rewrite_quarter_derived_from_prior_h1():
    filings = [{'accession_number':acc,'filed_at':datetime(2026,month,5,tzinfo=timezone.utc)}
        for acc,month in [('q1-original',5),('h1-original',8),('q1-restated',9)]]
    rows = [{'accn':'q1-original','start':'2026-01-01','end':'2026-03-31','val':100},
            {'accn':'h1-original','start':'2026-01-01','end':'2026-06-30','val':250},
            {'accn':'q1-restated','start':'2026-01-01','end':'2026-03-31','val':120}]
    raw = {'facts':{'us-gaap':facts('NetCashProvidedByUsedInOperatingActivities',rows)}}
    series = quarterly_series(raw,filings,NOW)
    q2,q1 = (quarter['metrics'][0] for quarter in series)
    assert q1['value']=='120'
    assert q2['value']=='150'
    assert {x['accession_number'] for x in q2['inputs']}=={'q1-original','h1-original'}
    assert q2['available_at']==datetime(2026,8,5,tzinfo=timezone.utc).isoformat()
    raw['facts']['us-gaap']['NetCashProvidedByUsedInOperatingActivities']['units']['USD'] = rows[1:]
    # A later-only revised prior period cannot supply the missing old basis.
    assert all(q['end']!='2026-06-30' for q in quarterly_series(raw,filings,NOW))
