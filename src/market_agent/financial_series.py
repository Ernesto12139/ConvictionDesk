"""Exact-period XBRL normalization and Decimal arithmetic, with input lineage."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext


CONCEPTS = {
    'revenue': ('USD', ['RevenueFromContractWithCustomerExcludingAssessedTax','Revenues','SalesRevenueNet']),
    'net_income': ('USD', ['NetIncomeLoss']),
    'operating_cash_flow': ('USD', ['NetCashProvidedByUsedInOperatingActivities']),
    'capex': ('USD', ['PaymentsToAcquirePropertyPlantAndEquipment']),
    'sbc': ('USD', ['ShareBasedCompensation']),
    'diluted_eps': ('USD/shares', ['EarningsPerShareDiluted'])}


def common_shares(raw, filings, cutoff):
    """Latest eligible entity-wide common-share instant, never weighted-average EPS shares.

    SEC CompanyFacts contains standard-taxonomy, entire-filing-entity facts.
    Conflicting values with the same period/acceptance are left unknown.
    """
    accepted = {r['accession_number']:r['filed_at'] for r in filings if r['filed_at'] <= cutoff}
    candidates = []
    for namespace, tag in [('us-gaap','CommonStockSharesOutstanding'),
                           ('dei','EntityCommonStockSharesOutstanding')]:
        for row in raw.get('facts',{}).get(namespace,{}).get(tag,{}).get('units',{}).get('shares',[]):
            if row.get('accn') not in accepted or row.get('form') not in {'10-Q','10-K'} or row.get('start'):
                continue
            try:
                end = date.fromisoformat(row['end'])
                value = Decimal(str(row['val']))
            except (KeyError, ValueError, TypeError, InvalidOperation):
                continue
            available = accepted[row['accn']]
            if (isinstance(row['val'],bool) or not value.is_finite() or value <= 0
                    or end > min(cutoff.date(),available.date())):
                continue
            candidates.append({'metric':'shares_outstanding','concept':namespace+':'+tag,
                'value':str(value),'unit':'shares','start':None,'end':str(end),
                'form':row['form'],'accession_number':row['accn'],
                'available_at':available.isoformat(),'scope':'ENTIRE_FILING_ENTITY'})
    if not candidates:
        return None
    latest = max((r['end'],r['available_at']) for r in candidates)
    selected = [r for r in candidates if (r['end'],r['available_at']) == latest]
    if len({Decimal(r['value']) for r in selected}) != 1:
        return None
    return selected[0]


def quarterly_series(raw, filings, cutoff):
    accepted = {row['accession_number']:row['filed_at'] for row in filings if row['filed_at'] <= cutoff}
    facts = raw.get('facts',{}).get('us-gaap',{})
    reported = {}
    for metric,(unit,tags) in CONCEPTS.items():
        for tag in tags:
            for row in facts.get(tag,{}).get('units',{}).get(unit,[]):
                if row.get('accn') not in accepted or row.get('form') not in {'10-Q','10-K'}:
                    continue
                try:
                    start,end = date.fromisoformat(row['start']),date.fromisoformat(row['end'])
                    value = Decimal(str(row['val']))
                except (KeyError,TypeError,ValueError,InvalidOperation):
                    continue
                if not value.is_finite() or not 60 <= (end-start).days <= 380 or end > cutoff.date():
                    continue
                fact = {'metric':metric,'concept':tag,'unit':unit,'start':str(start),'end':str(end),
                    'value':str(value),'accession_number':row['accn'],'available_at':accepted[row['accn']].isoformat()}
                # Preserve filing revisions until cumulative subtraction pairs
                # are selected. A later Q1 restatement cannot silently change
                # Q2 derived from an earlier, unrevised H1 disclosure.
                key = metric,tag,str(start),str(end),row['accn']
                prior = reported.get(key)
                if prior is None or fact['available_at'] > prior['available_at']:
                    reported[key] = fact
    quarters = {}

    def keep(fact, basis, inputs):
        key = fact['metric'],fact['start'],fact['end']
        candidate = {**fact,'basis':basis,'inputs':inputs}
        prior = quarters.get(key)
        # Prefer directly reported quarters and the most recently known filing.
        if prior is None or (basis=='REPORTED',fact['available_at']) > (prior['basis']=='REPORTED',prior['available_at']):
            quarters[key] = candidate

    for fact in reported.values():
        start,end = date.fromisoformat(fact['start']),date.fromisoformat(fact['end'])
        if (end-start).days <= 120:
            keep(fact,'REPORTED',[fact])
        elif fact['metric'] != 'diluted_eps':
            candidates = [prior for prior in reported.values() if prior['metric']==fact['metric']
                and prior['concept']==fact['concept'] and prior['unit']==fact['unit'] and prior['start']==fact['start']
                and prior['available_at'] <= fact['available_at']
                and 60 <= (end-date.fromisoformat(prior['end'])).days <= 120]
            if candidates:
                prior = max(candidates,key=lambda item:(item['end'],
                    item['accession_number']==fact['accession_number'],item['available_at']))
                with localcontext() as ctx:
                    ctx.prec = 50
                    value = Decimal(fact['value'])-Decimal(prior['value'])
                keep({**fact,'start':str(date.fromisoformat(prior['end'])+timedelta(days=1)),
                    'value':str(value),'available_at':max(fact['available_at'],prior['available_at'])},
                    'YTD_DIFFERENCE',[prior,fact])
    periods = sorted({(fact['start'],fact['end']) for fact in quarters.values()},key=lambda pair:pair[1],reverse=True)[:4]
    result = []
    for start,end in periods:
        metrics = [fact for (_,s,e),fact in quarters.items() if (s,e)==(start,end)]
        by_name = {row['metric']:row for row in metrics}
        if 'operating_cash_flow' in by_name and 'capex' in by_name and Decimal(by_name['capex']['value']) >= 0:
            ocf,capex = by_name['operating_cash_flow'],by_name['capex']
            with localcontext() as ctx:
                ctx.prec = 50
                fcf = Decimal(ocf['value'])-Decimal(capex['value'])
            metrics.append({'metric':'free_cash_flow','concept':'OCF_MINUS_CAPEX','unit':'USD',
                'start':start,'end':end,'value':str(fcf),'basis':'PYTHON_DERIVED',
                'available_at':max(ocf['available_at'],capex['available_at']),
                'inputs':[ *ocf['inputs'], *capex['inputs']]})
        result.append({'start':start,'end':end,'metrics':metrics})
    return result


def valuation_reference(*, price=None, ttm_eps=None, market_cap=None, debt=None, cash=None):
    def known(value):
        if value is None:
            return None
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError('Valuation inputs must be finite')
        return result
    price,eps,cap,debt,cash = map(known,(price,ttm_eps,market_cap,debt,cash))
    if any(value is not None and value < 0 for value in (price,cap,debt,cash)):
        raise ValueError('Nonnegative price, market cap, debt and cash required')
    with localcontext() as ctx:
        ctx.prec = 50
        pe = price/eps if price is not None and eps is not None and eps > 0 else None
        ev = cap+debt-cash if all(value is not None for value in (cap,debt,cash)) else None
    return {'pe':str(pe) if pe is not None else None,'enterprise_value':str(ev) if ev is not None else None,
        'pe_status':'AVAILABLE' if pe is not None else 'NOT_MEANINGFUL' if eps is not None and eps <= 0 else 'UNKNOWN',
        'basis':'CALLER_MUST_SUPPLY_QUALIFIED_SAME_CURRENCY_INPUTS'}
