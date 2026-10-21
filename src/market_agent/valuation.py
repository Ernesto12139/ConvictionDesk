"""Read-only, source-bound valuation references over a frozen research task.

No provider/model calls and no history writes. Reported shares are dated inputs,
not a continuously updated capitalization feed. Unknowns never become zero.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import Literal

from pydantic import field_validator, model_validator

from .agent import MAX_EOD_AGE
from .domain import EvidenceItem, FrozenModel, utc
from .market_evidence import NEW_YORK


class ValuationMetric(FrozenModel):
    value: Decimal | None = None
    unit: str
    status: Literal['AVAILABLE', 'REFERENCE', 'UNKNOWN', 'NOT_MEANINGFUL'] = 'UNKNOWN'
    reason: str
    evidence_ids: list[str] = []
    currency: str | None = None
    period: dict | None = None
    as_of: datetime | None = None
    source_id: list[str] = []
    data_status: str | None = None
    display_value: str | None = None
    display_approximate: bool = False

    @field_validator('as_of')
    @classmethod
    def aware_asof(cls, value):
        return utc(value) if value is not None else None

    @model_validator(mode='after')
    def provenance_aliases(self):
        object.__setattr__(self,'currency','USD' if self.unit in {'USD','USD/shares'} else None)
        object.__setattr__(self,'source_id',list(self.evidence_ids))
        object.__setattr__(self,'data_status',self.status)
        if self.value is not None:
            with localcontext() as context:
                context.rounding=ROUND_HALF_EVEN
                display=(format(self.value,'.3E') if 0<self.value.copy_abs()<Decimal('.01') else format(self.value,'.2f')) if self.unit=='multiple' else str(self.value)
            object.__setattr__(self,'display_value',display)
            object.__setattr__(self,'display_approximate',Decimal(display)!=self.value)
        return self


class ValuationReport(FrozenModel):
    version: Literal['frozen-valuation-v1'] = 'frozen-valuation-v1'
    symbol: str | None
    task_id: str
    revision: int
    cutoff: datetime
    basis: Literal['FROZEN_TASK_INPUTS_NO_REFRESH'] = 'FROZEN_TASK_INPUTS_NO_REFRESH'
    price_reference: dict | None = None
    shares_reference: dict | None = None
    quarters: list[dict] = []
    ttm_period: dict | None = None
    capital_basis_status: Literal['UNKNOWN','VERIFIED_NO_CHANGE','REQUIRES_RECONCILIATION'] = 'UNKNOWN'
    capital_basis_evidence_ids: list[str] = []
    metrics: dict[str, ValuationMetric]
    rejected_inputs: list[str] = []
    limitations: list[str]


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    try:
        result = Decimal(value)
    except (ValueError, InvalidOperation):
        return None
    return result if result.is_finite() else None


def known_time(value, cutoff):
    try:
        result = utc(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return None
    return result if result <= cutoff else None


def matches_original_inputs(row):
    """Recheck the supported quarter derivation, not just its self-reported hash."""
    inputs = row.get('inputs')
    if not isinstance(inputs,list) or not inputs or any(not isinstance(x,dict) for x in inputs):
        return False
    if any(x.get('concept',row.get('concept')) != row.get('concept') for x in inputs):
        return False
    if row.get('basis') == 'REPORTED':
        return (len(inputs)==1 and inputs[0].get('start')==row.get('start')
                and inputs[0].get('end')==row.get('end')
                and number(inputs[0].get('value'))==number(row.get('value')))
    if row.get('basis') != 'YTD_DIFFERENCE' or len(inputs)!=2:
        return False
    try:
        prior,current = sorted(inputs,key=lambda x:x['end'])
        aligned = (prior['start']==current['start'] and current['end']==row['end']
            and date.fromisoformat(prior['end'])+timedelta(days=1)==date.fromisoformat(row['start']))
    except (KeyError,TypeError,ValueError):
        return False
    if not aligned or number(prior.get('value')) is None or number(current.get('value')) is None:
        return False
    with localcontext() as ctx:
        ctx.prec=50
        return number(current['value'])-number(prior['value'])==number(row.get('value'))


def capital_basis(sources,price,share,quarters,period,cutoff):
    """Require a hash-bound provider action/class check; no live adapter emits it yet.

    A flag/model assertion alone cannot qualify. The check is tied to this price,
    reported shares and exact financial inputs, with a complete coverage window.
    Synthetic fixtures exercise the contract; real missing checks stay UNKNOWN.
    """
    from .tdb import content_hash
    if not price or not share or price['session'] not in {'COMPLETED_CLOSE','REGULAR'}:
        return 'UNKNOWN',[]
    by_id={e.evidence_id:e for e in sources}
    evidence=by_id[price['evidence_id']];data=evidence.structured_data
    check=data.get('capital_basis_check')
    if (evidence.provider not in {'massive','synthetic'} or not isinstance(check,dict)
            or content_hash(data)!=evidence.content_hash or data.get('price_basis')!='unadjusted'):
        return 'UNKNOWN',[]
    required={share['evidence_id'],*[q['evidence_id'] for q in quarters]}
    first=min(share['end'],period['start'] if period else share['end'])
    last=datetime.fromisoformat(price['as_of']).date()
    try:
        start,end=date.fromisoformat(check['from_date']),date.fromisoformat(check['through_date'])
        known=known_time(check['known_at'],cutoff)
    except (ValueError,KeyError,TypeError):
        return 'UNKNOWN',[]
    hashes=check.get('input_hashes')
    if (not known or known.date()<end or evidence.retrieved_at<known
            or start>date.fromisoformat(first) or end<last or end>cutoff.date()
            or check.get('coverage_complete') is not True or not isinstance(hashes,dict)
            or any(hashes.get(key)!=by_id[key].content_hash for key in required)
            or number(check.get('price_value'))!=number(price['value'])
            or known_time(check.get('price_as_of'),cutoff)!=evidence.as_of
            or check.get('price_session')!=price['session']
            or check.get('share_count_from_date')!=share['end']
            or not isinstance(check.get('source_uri'),str) or not check['source_uri'].startswith('https://')):
        return 'UNKNOWN',[]
    if (check.get('split_status')=='CHANGE_DETECTED' or check.get('share_count_status')=='CHANGE_DETECTED'
            or check.get('share_class_alignment')=='MISMATCH'):
        return 'REQUIRES_RECONCILIATION',[evidence.evidence_id]
    if (check.get('split_status')=='NO_CHANGE' and check.get('share_count_status')=='NO_CHANGE'
            and check.get('share_class_alignment')=='VERIFIED'):
        return 'VERIFIED_NO_CHANGE',[evidence.evidence_id]
    return 'UNKNOWN',[]


def valuation_report(task):
    cutoff = utc(task.cutoff)
    sources = [s.evidence for s in task.sources if s.identity == 'VERIFIED_FACT'
               and s.evidence.symbol == task.focus_symbol]
    source_ids = {e.evidence_id for e in sources if e.evidence_type=='sec_filing'
        and e.provider=='sec_edgar' and e.published_at and e.published_at<=cutoff
        and (not e.as_of or e.as_of<=cutoff)
        and all(not t or e.retrieved_at>=t for t in (e.published_at,e.as_of))}
    prices, shares, quarters, rejected = [], [], [], []
    for e in sources:
        data = e.structured_data
        if not e.as_of and e.evidence_type == 'market_data':
            rejected.append(e.evidence_id+':PRICE_TIME_UNKNOWN')
            continue
        if e.published_at and e.published_at > cutoff or e.as_of and e.as_of > cutoff:
            rejected.append(e.evidence_id+':FUTURE_INPUT')
            continue
        if any(t and e.retrieved_at < t for t in (e.published_at,e.as_of)):
            rejected.append(e.evidence_id+':INVALID_RETRIEVAL_TIME')
            continue
        if e.evidence_type == 'market_data':
            price = number(data.get('price'))
            if (price is None or price <= 0 or data.get('currency') != 'USD'
                    or data.get('symbol') != task.focus_symbol or cutoff-e.as_of > MAX_EOD_AGE):
                rejected.append(e.evidence_id+':UNQUALIFIED_PRICE')
                continue
            session = data.get('session') or ('COMPLETED_CLOSE' if data.get('session_date') else 'UNKNOWN')
            if data.get('session_date'):
                try:
                    day = date.fromisoformat(data['session_date'])
                    if day != e.as_of.astimezone(NEW_YORK).date():
                        raise ValueError('session date mismatch')
                except (TypeError,ValueError):
                    rejected.append(e.evidence_id+':INVALID_SESSION_DATE')
                    continue
            # An unlabelled price cannot silently become a regular-session/live quote.
            prices.append({'value':str(price),'unit':'USD/shares','as_of':e.as_of.isoformat(),
                'currency':'USD','period':{'instant':e.as_of.isoformat()},
                'source_id':[e.evidence_id],'data_status':'REFERENCE',
                'retrieved_at':e.retrieved_at.isoformat(),'session':session,
                'session_date':data.get('session_date'),'feed_delay':'UNKNOWN',
                'provider':e.provider,'source_uri':e.source_uri,'evidence_id':e.evidence_id})
        if e.evidence_type == 'sec_filing' and e.provider == 'sec_edgar':
            legacy_rows = data.get('metrics', [])
            share_rows = data.get('share_facts', [])
            if (data.get('symbol') != task.focus_symbol or not isinstance(legacy_rows,list)
                    or not isinstance(share_rows,list)):
                rejected.append(e.evidence_id+':INVALID_SHARE_SOURCE')
                continue
            rows=[*legacy_rows,*share_rows]  # Read original 8f5b22c bytes without rewriting them.
            for row in rows:
                if not isinstance(row,dict):
                    continue
                if row.get('metric') != 'shares_outstanding':
                    continue
                amount = number(row.get('value'))
                available = known_time(row.get('available_at'), cutoff)
                try:
                    end = date.fromisoformat(row['end'])
                except (ValueError, KeyError, TypeError):
                    continue
                if (amount is None or amount <= 0 or row.get('unit') != 'shares'
                        or not available or not e.published_at or available > e.published_at
                        or end > min(cutoff.date(),available.date()) or (cutoff.date()-end).days > 120
                        or row.get('scope') != 'ENTIRE_FILING_ENTITY'):
                    rejected.append(e.evidence_id+':UNQUALIFIED_SHARES')
                    continue
                shares.append({**row,'provider':e.provider,'source_uri':e.source_uri,
                               'currency':None,'period':{'instant':row['end']},'as_of':row['end'],
                               'source_id':[e.evidence_id],'data_status':'REPORTED_DATED',
                               'evidence_id':e.evidence_id,'retrieved_at':e.retrieved_at.isoformat()})
        if e.provider == 'python_decimal' and data.get('calculation') == 'sec_quarterly_v1':
            from .tdb import content_hash
            parents = data.get('source_evidence_ids')
            if (content_hash(data) != e.content_hash or data.get('symbol') != task.focus_symbol
                    or not isinstance(parents,list) or not parents or any(not isinstance(p,str) for p in parents)
                    or set(parents)-source_ids):
                rejected.append(e.evidence_id+':QUARTER_INTEGRITY')
                continue
            q = data.get('quarter', {})
            if not isinstance(q,dict):
                rejected.append(e.evidence_id+':INVALID_QUARTER')
                continue
            try:
                start, end = date.fromisoformat(q['start']), date.fromisoformat(q['end'])
            except (ValueError, KeyError, TypeError):
                continue
            rows = q.get('metrics', [])
            if (not isinstance(rows,list) or any(not isinstance(r,dict) or not isinstance(r.get('metric'),str) for r in rows)
                    or not 60 <= (end-start).days <= 120 or end > cutoff.date()
                    or not e.published_at or len({r['metric'] for r in rows}) != len(rows)):
                rejected.append(e.evidence_id+':QUARTER_PERIOD_OR_METRICS')
                continue
            qualified = []
            for row in rows:
                available = known_time(row.get('available_at'), cutoff)
                if (row.get('start') == q['start'] and row.get('end') == q['end']
                        and number(row.get('value')) is not None and available
                        and end <= available.date() and available <= e.published_at):
                    qualified.append(row)
            quarters.append({'start':q['start'],'end':q['end'],'metrics':qualified,
                             'evidence_id':e.evidence_id,'source_uri':e.source_uri})
    regular=[p for p in prices if p['session'] in {'COMPLETED_CLOSE','REGULAR'}]
    price = max(regular or prices, key=lambda p:p['as_of']) if prices else None
    share = max(shares, key=lambda p:(p['end'], p['available_at'])) if shares else None
    if price and len({(number(p['value']),p['session'],p['session_date']) for p in prices if p['as_of']==price['as_of']})>1:
        rejected.append('CONFLICTING_LATEST_PRICES');price=None
    if share and len({number(s['value']) for s in shares if (s['end'],s['available_at'])==(share['end'],share['available_at'])})>1:
        rejected.append('CONFLICTING_LATEST_SHARES');share=None
    quarters.sort(key=lambda q:q['end'])
    # Competing period versions are not implicitly combined into a precise TTM.
    unique_periods = len({q['end'] for q in quarters}) == len(quarters)
    quarters = quarters[-4:]
    contiguous = (unique_periods and len(quarters) == 4 and all(
        date.fromisoformat(a['end'])+timedelta(days=1) == date.fromisoformat(b['start'])
        for a,b in zip(quarters,quarters[1:])))
    period = {'start':quarters[0]['start'],'end':quarters[-1]['end']} if contiguous else None
    basis_status,basis_ids=capital_basis(sources,price,share,quarters,period,cutoff)
    metrics = {}
    for name, unit in [('revenue','USD'),('net_income','USD'),
                       ('operating_cash_flow','USD'),('diluted_eps','USD/shares')]:
        key = 'ttm_'+name
        rows = [next((m for m in q['metrics'] if m['metric'] == name),None) for q in quarters]
        reason = 'FOUR_CONTIGUOUS_QUALIFIED_QUARTERS_REQUIRED'
        if contiguous and all(r and r.get('unit') == unit for r in rows):
            basis = {}
            compatible = (all(isinstance(r.get('concept'),str) and r['concept'] for r in rows)
                          and len({r.get('concept') for r in rows}) == 1)
            for row in rows:
                inputs = row.get('inputs')
                if (not isinstance(inputs,list) or not inputs or
                        any(not isinstance(x,dict) or x.get('metric') != name or x.get('unit') != unit
                            or number(x.get('value')) is None
                            or not known_time(x.get('available_at'),cutoff)
                            or known_time(x.get('available_at'),cutoff) > known_time(row['available_at'],cutoff)
                            for x in inputs)):
                    compatible = False
                    continue
                if not matches_original_inputs(row):
                    compatible = False
                for original in inputs:
                    k = (original.get('metric'), original.get('start'), original.get('end'))
                    value = number(original.get('value'))
                    if k in basis and basis[k] != value:
                        compatible = False
                    basis[k] = value
            if name == 'diluted_eps' and any(r.get('basis') != 'REPORTED' for r in rows):
                compatible = False
            if name == 'diluted_eps' and basis_status!='VERIFIED_NO_CHANGE':
                metrics[key]=ValuationMetric(unit=unit,reason='CAPITAL_BASIS_UNVERIFIED',period=period)
                continue
            if compatible:
                with localcontext() as ctx:
                    ctx.prec = 50
                    total = sum((number(r['value']) for r in rows),Decimal(0))
                metrics[key] = ValuationMetric(value=total,unit=unit,status='AVAILABLE',
                    reason='EXACT_PERIOD_SUM_NO_EPS_YTD_SUBTRACTION',evidence_ids=[q['evidence_id'] for q in quarters],
                    period=period,as_of=max(known_time(r['available_at'],cutoff) for r in rows))
                continue
            reason = 'INCOMPATIBLE_CONCEPT_OR_RESTATEMENT_BASIS'
        metrics[key] = ValuationMetric(unit=unit,reason=reason)
    cap = None
    if price and share and basis_status=='VERIFIED_NO_CHANGE':
        # Never use a share observation that post-dates the price reference.
        if date.fromisoformat(share['end']) <= datetime.fromisoformat(price['as_of']).date():
            with localcontext() as ctx:
                ctx.prec = 50
                cap = number(price['value'])*number(share['value'])
    metrics['market_cap_reference'] = ValuationMetric(value=cap,unit='USD',
        status='REFERENCE' if cap is not None else 'UNKNOWN',
        reason='PRICE_TIMES_DATED_REPORTED_COMMON_SHARES' if cap is not None else 'CAPITAL_BASIS_UNVERIFIED' if price and share else 'QUALIFIED_PRICE_AND_SHARES_REQUIRED',
        evidence_ids=list(dict.fromkeys([price['evidence_id'],share['evidence_id'],*basis_ids])) if cap is not None else [],
        period={'price_as_of':price['as_of'],'shares_reported_end':share['end']} if cap is not None else None,
        as_of=datetime.fromisoformat(price['as_of']) if cap is not None else None)
    fresh_financials = period and (cutoff.date()-date.fromisoformat(period['end'])).days <= 180
    for key, numerator, denominator, ids in [
        ('pe_ttm',number(price['value']) if price else None,metrics['ttm_diluted_eps'].value,
         [price['evidence_id']] if price else []),
        ('price_sales_ttm',cap,metrics['ttm_revenue'].value,
         metrics['market_cap_reference'].evidence_ids)]:
        metric_ids = metrics['ttm_diluted_eps' if key=='pe_ttm' else 'ttm_revenue'].evidence_ids
        financial_asof=metrics['ttm_diluted_eps' if key=='pe_ttm' else 'ttm_revenue'].as_of
        value = None
        if numerator is not None and denominator is not None and denominator > 0 and fresh_financials and basis_status=='VERIFIED_NO_CHANGE':
            with localcontext() as ctx:
                ctx.prec = 50
                value = numerator/denominator
        meaningless = denominator is not None and denominator <= 0
        metrics[key] = ValuationMetric(value=value,unit='multiple',
            status='REFERENCE' if value is not None else 'NOT_MEANINGFUL' if meaningless else 'UNKNOWN',
            reason='DATED_INPUT_REFERENCE_ONLY' if value is not None else 'NONPOSITIVE_DENOMINATOR' if meaningless else 'CAPITAL_BASIS_UNVERIFIED' if basis_status!='VERIFIED_NO_CHANGE' else 'QUALIFIED_ALIGNED_INPUTS_REQUIRED',
            evidence_ids=list(dict.fromkeys(ids+metric_ids+(basis_ids if value is not None else []))) if value is not None or meaningless else [],
            period=period if denominator is not None else None,
            as_of=max([t for t in [financial_asof,datetime.fromisoformat(price['as_of']) if price else None] if t])
                if denominator is not None and financial_asof else None)
    metrics['enterprise_value'] = ValuationMetric(unit='USD',reason='QUALIFIED_TOTAL_DEBT_AND_ALIGNED_CASH_NOT_AVAILABLE')
    return ValuationReport(symbol=task.focus_symbol,task_id=task.task_id,revision=task.revision,
        cutoff=cutoff,price_reference=price,shares_reference=share,quarters=quarters,
        ttm_period=period,metrics=metrics,capital_basis_status=basis_status,
        capital_basis_evidence_ids=basis_ids,rejected_inputs=rejected,limitations=[
            'Read-only projection of this frozen task; no source refresh or new research revision.',
            'Price is a dated close/quote reference, never a real-time feed guarantee; delay may be unknown.',
            'Capitalization/multiples require a complete source-backed split/share-class basis check; unknown or changed basis disables them.',
            'Four exact historical quarters do not imply current guidance or investment correctness.',
            'EV, forward multiples, growth forecasts and valuation scenarios remain unavailable.'])


def valuation_evidence(task):
    """A citeable Python receipt; no model arithmetic, fetch or saved-task update."""
    from .tdb import content_hash
    report=valuation_report(task)
    if not any(metric.value is not None for metric in report.metrics.values()):
        return None
    fields={'value','unit','currency','period','as_of','source_id','data_status','reason'}
    metrics={key:{k:v for k,v in row.model_dump(mode='json').items() if k in fields}
             for key,row in report.metrics.items()}
    parents=list(dict.fromkeys(eid for metric in report.metrics.values() for eid in metric.evidence_ids))
    sources=[s.evidence for s in task.sources if s.evidence.evidence_id in parents]
    times=[stamp for e in sources for stamp in (e.published_at,e.as_of) if stamp]
    if not times:
        return None
    available=max(times)
    payload={'calculation':'task_valuation_v1','symbol':task.focus_symbol,
        'cutoff':task.cutoff.isoformat(),'basis':report.basis,'metrics':metrics,
        'price_reference':report.price_reference,'shares_reference':report.shares_reference,
        'capital_basis_status':report.capital_basis_status,
        'source_evidence_ids':parents,
        'limitations':['Dated reference only; reported shares are not reconciled current shares.',
                      'No live price guarantee, EV, forecasts or valuation recommendation.']}
    digest=content_hash(payload)
    text='; '.join(f"{key} {row.value} {row.unit}" if row.value is not None else f"{key} {row.status}"
                  for key,row in report.metrics.items())
    if report.ttm_period:
        text+='; historical TTM period '+report.ttm_period['start']+' / '+report.ttm_period['end']
    return EvidenceItem(evidence_id='E'+digest[7:23],evidence_type='calculation',provider='python_decimal',
        symbol=task.focus_symbol,title='Python · 冻结来源估值与 TTM 参考',content_hash=digest,
        published_at=available,as_of=max((m.as_of for m in report.metrics.values() if m.as_of),default=available),
        retrieved_at=max([task.cutoff,*[e.retrieved_at for e in sources]]),excerpt=text,structured_data=payload)
