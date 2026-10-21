"""Local, non-persisting what-if receipts over the existing frozen SEC engine.

User prices and assumptions never become independently verified market facts.
This module neither fetches nor changes ResearchTask/Action/Deep context.
"""
from datetime import date, datetime
from decimal import Decimal, localcontext
from typing import Literal

from pydantic import Field, field_validator

from .domain import EvidenceItem, FrozenModel, utc
from .market_evidence import NEW_YORK
from .tdb import content_hash
from .valuation import ValuationMetric, known_time, number, valuation_report

# Published NYSE full-closure dates, checked against the official calendar.
# No live query, emergency-halt guarantee or assumed 16:00 timestamp follows.
CALENDAR_SOURCE='https://www.nyse.com/trade/hours-calendars'
CLOSED_DATES={
    2026:set('01-01 01-19 02-16 04-03 05-25 06-19 07-03 09-07 11-26 12-25'.split()),
    2027:set('01-01 01-18 02-15 03-26 05-31 06-18 07-05 09-06 11-25 12-24'.split()),
    2028:set('01-17 02-21 04-14 05-29 06-19 07-04 09-04 11-23 12-25'.split()),
}


class CapitalAssumptions(FrozenModel):
    unchanged_since_share_date: bool = False
    entire_common_equity_at_input_class_price: bool = False
    consolidated_income_available_to_common: bool = False


class UserPriceRequest(FrozenModel):
    revision: int = Field(ge=1)
    symbol: str = Field(pattern=r'^[A-Z][A-Z0-9.\-]{0,14}$')
    price: Decimal = Field(gt=0, le=Decimal('1000000000000'), max_digits=24, decimal_places=10)
    comparison_price: Decimal | None = Field(default=None, gt=0, le=Decimal('1000000000000'),
        max_digits=24, decimal_places=10, description='Optional user-defined scenario price, never a fetched market observation.')
    currency: Literal['USD'] = 'USD'
    price_date: date
    session: Literal['COMPLETED_CLOSE', 'REGULAR', 'HYPOTHETICAL']
    adjustment_basis: Literal['UNADJUSTED'] = 'UNADJUSTED'
    kind: Literal['USER_SUPPLIED_OBSERVATION', 'USER_SUPPLIED_SCENARIO']
    source_basis: Literal['OWN_OBSERVATION', 'HYPOTHETICAL', 'THIRD_PARTY']
    source_note: str = Field(min_length=1, max_length=400)
    local_calculation_rights_attested: bool = False
    assumptions: CapitalAssumptions = CapitalAssumptions()

    @field_validator('price', 'comparison_price', mode='before')
    @classmethod
    def exact_input(cls, value):
        if value is None:
            return value
        if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
            raise ValueError('Use an exact decimal string')
        return value

    @field_validator('source_note')
    @classmethod
    def note(cls, value):
        if not value.strip():
            raise ValueError('Source note required')
        return value.strip()


class PriceEvidence(FrozenModel):
    """Minimal adapter output. Only server code may admit provider rights.

    No provider adapter is enabled by this contract. The HTTP input accepts
    UserPriceRequest only, so a client cannot self-promote to licensed/verified.
    """
    provider_identity: str
    instrument_identity: str
    price: Decimal
    currency: Literal['USD']
    session: str
    effective_date: date
    retrieved_at: datetime
    adjustment_basis: str
    usage_rights: Literal['USER_ATTESTED_LOCAL_ONLY', 'SERVER_VERIFIED_DERIVATION', 'DISPLAY_ONLY', 'UNKNOWN']
    source_id: str
    trust_status: Literal['USER_ASSERTION', 'SYNTHETIC', 'PROVIDER_REPORTED']
    external_model_use: Literal['NOT_AUTHORIZED'] = 'NOT_AUTHORIZED'

    _retrieved = field_validator('retrieved_at')(utc)

    @field_validator('price', mode='before')
    @classmethod
    def exact_price(cls, value):
        return UserPriceRequest.exact_input(value)

    @field_validator('price')
    @classmethod
    def finite_positive(cls, value):
        if not value.is_finite() or value <= 0:
            raise ValueError('Invalid price')
        return value


def derivation_allowed(price):
    return (price.usage_rights == 'SERVER_VERIFIED_DERIVATION'
            or price.usage_rights == 'USER_ATTESTED_LOCAL_ONLY' and price.trust_status == 'USER_ASSERTION')


def dated_valuation(task, request: UserPriceRequest, evaluated_at: datetime):
    """Return a detached analysis context, with its own citeable local receipts.

    An observation remains user asserted; unknown capital continuity is always
    conditional. Known conflicting evidence overrides the user's assumptions.
    """
    evaluated_at = utc(evaluated_at)
    report = valuation_report(task)
    reasons = []
    qualification_notes = []
    if request.symbol != task.focus_symbol or request.revision != task.revision:
        reasons.append('TASK_INSTRUMENT_OR_REVISION_MISMATCH')
    if task.status != 'COMPLETE':
        reasons.append('COMPLETED_RESEARCH_REQUIRED')
    if evaluated_at < task.cutoff:
        reasons.append('EVALUATION_BEFORE_RESEARCH_CUTOFF')
    if request.price_date > task.cutoff.astimezone(NEW_YORK).date():
        reasons.append('PRICE_AFTER_RESEARCH_CUTOFF_CREATE_NEW_CONTEXT')
    observation = request.kind == 'USER_SUPPLIED_OBSERVATION'
    if observation:
        if request.source_basis != 'OWN_OBSERVATION' or request.session == 'HYPOTHETICAL':
            reasons.append('OBSERVATION_SOURCE_OR_SESSION_MISMATCH')
        if request.price_date.weekday() >= 5:
            reasons.append('OBSERVATION_NON_WEEKDAY')
        if request.price_date.year not in CLOSED_DATES:
            reasons.append('OBSERVATION_CALENDAR_COVERAGE_UNAVAILABLE')
        elif request.price_date.strftime('%m-%d') in CLOSED_DATES[request.price_date.year]:
            reasons.append('OBSERVATION_PUBLISHED_MARKET_CLOSURE')
        # Date-only input cannot prove a completed observation before an intraday cutoff.
        if request.price_date == task.cutoff.astimezone(NEW_YORK).date():
            reasons.append('DATE_ONLY_OBSERVATION_REQUIRES_PRIOR_MARKET_DATE')
    elif request.source_basis != 'HYPOTHETICAL' or request.session != 'HYPOTHETICAL':
        reasons.append('SCENARIO_MUST_BE_EXPLICITLY_HYPOTHETICAL')
    if not request.local_calculation_rights_attested:
        reasons.append('LOCAL_DERIVATION_RIGHTS_NOT_ATTESTED')
    if request.source_basis == 'THIRD_PARTY':
        reasons.append('SERVER_VERIFIED_PROVIDER_DERIVATION_RIGHTS_REQUIRED')
    # A known restricted feed cannot be relabelled as an owned observation.
    import re
    if re.search(r'\b(?:massive|polygon(?:\.io)?)\b', request.source_note, re.I):
        reasons.append('DISPLAY_ONLY_PROVIDER_NOT_ADMITTED_FOR_DERIVATION')
    share = report.shares_reference
    share_source = None
    if share:
        share_source = next((s.evidence for s in task.sources if s.evidence.evidence_id == share['evidence_id']), None)
        valid_concepts = {'us-gaap:CommonStockSharesOutstanding', 'dei:EntityCommonStockSharesOutstanding'}
        if share.get('concept') not in valid_concepts or not share.get('accession_number'):
            reasons.append('SEC_COMMON_SHARE_CONCEPT_AND_ACCESSION_REQUIRED')
        count=number(share.get('value'))
        if count is None or count != count.to_integral_value() or not 1 <= count <= Decimal('1e18'):
            reasons.append('SEC_COMMON_SHARE_COUNT_INVALID')
        if not share_source or content_hash(share_source.structured_data) != share_source.content_hash:
            reasons.append('SEC_SHARE_SOURCE_INTEGRITY')
        share_date = date.fromisoformat(share['end'])
        share_age = (request.price_date-share_date).days
        if not 0 <= share_age <= 120:
            reasons.append('SHARE_DATE_MUST_PRECEDE_PRICE_WITHIN_120_DAYS')
        elif share_age > 100:
            qualification_notes.append('SHARES_OLDER_THAN_100_DAYS_SCENARIO_ONLY')
        available = datetime.fromisoformat(share['available_at'])
        if available.astimezone(NEW_YORK).date() > request.price_date:
            reasons.append('SHARE_FILING_NOT_PUBLIC_BY_PRICE_DATE')
        if observation and available.astimezone(NEW_YORK).date() == request.price_date:
            reasons.append('DATE_ONLY_PRICE_VS_FILING_TIME_AMBIGUOUS')
    else:
        reasons.append('QUALIFIED_DATED_SEC_COMMON_SHARES_REQUIRED')
    if report.capital_basis_status == 'REQUIRES_RECONCILIATION':
        reasons.append('KNOWN_CAPITAL_BASIS_CONFLICT')
    listing_source_ids = []
    for source in task.sources:
        if source.identity != 'VERIFIED_FACT' or source.evidence.symbol != task.focus_symbol:
            continue
        data = source.evidence.structured_data or {}
        # Optional source-backed qualification, never supplied by this form.
        if source.evidence.provider == 'sec_edgar':
            classes = data.get('listed_common_symbols')
            if isinstance(classes, list) and (len(set(classes)) != 1 or classes != [task.focus_symbol]):
                reasons.append('MULTI_CLASS_OR_INSTRUMENT_SCOPE_UNRECONCILED')
            listing = data.get('issuer_listing_identity')
            if isinstance(listing, dict) and isinstance(listing.get('tickers'), list):
                observed = known_time(listing.get('observed_at'), task.cutoff)
                if observed and (len(set(listing['tickers'])) != 1 or listing['tickers'] != [task.focus_symbol]):
                    reasons.append('MULTI_CLASS_OR_INSTRUMENT_SCOPE_UNRECONCILED')
                if (observed and listing['tickers'] == [task.focus_symbol]
                        and observed.astimezone(NEW_YORK).date() <= request.price_date
                        and (not observation or observed.astimezone(NEW_YORK).date() < request.price_date)
                        and observed <= source.evidence.retrieved_at <= task.cutoff
                        and source.evidence.published_at and source.evidence.published_at <= task.cutoff
                        and content_hash(data) == source.evidence.content_hash):
                    listing_source_ids.append(source.evidence.evidence_id)
        check = data.get('capital_basis_check')
        if isinstance(check, dict) and (check.get('split_status') == 'CHANGE_DETECTED'
                or check.get('share_count_status') == 'CHANGE_DETECTED'
                or check.get('share_class_alignment') == 'MISMATCH'):
            reasons.append('KNOWN_CAPITAL_ACTION_OR_CLASS_CONFLICT')
    assumptions = request.assumptions
    if not assumptions.unchanged_since_share_date or not assumptions.entire_common_equity_at_input_class_price:
        reasons.append('EXPLICIT_CAPITAL_CONTINUITY_AND_CLASS_ASSUMPTIONS_REQUIRED')
    qualification = 'CONDITIONAL_REFERENCE' if observation else 'ILLUSTRATIVE_SCENARIO'
    if not listing_source_ids:
        qualification_notes.append('MISSING_POINT_IN_TIME_SINGLE_LISTING_IDENTITY')
    if qualification_notes:
        qualification = 'ILLUSTRATIVE_SCENARIO'
    reasons = list(dict.fromkeys(reasons))
    if reasons:
        qualification = 'UNAVAILABLE'
    input_payload = request.model_dump(mode='json')
    input_payload.update(identity='USER_ASSERTION', received_at=evaluated_at.isoformat(),
        usage_rights='USER_ATTESTED_LOCAL_ONLY' if request.local_calculation_rights_attested else 'UNKNOWN',
        rights_verification='USER_ATTESTATION_NOT_INDEPENDENTLY_VERIFIED')
    input_hash = content_hash(input_payload)
    price_evidence = PriceEvidence(provider_identity='user', instrument_identity=request.symbol,
        price=request.price, currency=request.currency, session=request.session, effective_date=request.price_date,
        retrieved_at=evaluated_at, adjustment_basis=request.adjustment_basis,
        usage_rights=input_payload['usage_rights'], source_id='E'+input_hash[7:23], trust_status='USER_ASSERTION')
    if not derivation_allowed(price_evidence) and 'LOCAL_DERIVATION_RIGHTS_NOT_ATTESTED' not in reasons:
        reasons.append('DERIVATION_NOT_AUTHORIZED');qualification='UNAVAILABLE'
    price_receipt = EvidenceItem(evidence_id=price_evidence.source_id, evidence_type='market_data', provider='user',
        symbol=request.symbol, title='用户日期化价格 · 未独立核实', retrieved_at=evaluated_at,
        content_hash=input_hash, structured_data=input_payload)
    cap = None
    if qualification != 'UNAVAILABLE':
        with localcontext() as ctx:
            ctx.prec = 50
            cap = request.price * number(share['value'])
    parents = list(dict.fromkeys([price_receipt.evidence_id] + ([share['evidence_id']] if share else []) + listing_source_ids))
    common = dict(status='REFERENCE' if cap is not None else 'UNKNOWN', reason=qualification,
        evidence_ids=parents, as_of=evaluated_at,
        period={'price_as_of':request.price_date.isoformat(), 'shares_as_of':share['end'] if share else None})
    metrics = {'reference_equity_value':ValuationMetric(value=cap, unit='USD', **common)}
    period = report.ttm_period
    financial_period_ok = bool(period and 0 <= (request.price_date-date.fromisoformat(period['end'])).days <= 180)
    for key, denominator_key in [('reference_ps','ttm_revenue'), ('reference_pe','ttm_net_income')]:
        denominator = report.metrics[denominator_key]
        value = None
        status = 'UNKNOWN'
        reason = 'QUALIFIED_ALIGNED_TTM_REQUIRED'
        attributable = key != 'reference_pe' or assumptions.consolidated_income_available_to_common
        # Retain source exact-period checks; never use summed cross-period EPS.
        financial_available = (denominator.as_of is not None
            and denominator.as_of.astimezone(NEW_YORK).date() <= request.price_date)
        eligible = cap is not None and denominator.value is not None and financial_period_ok and financial_available
        if eligible and attributable:
            if denominator.value <= 0:
                status='NOT_MEANINGFUL';reason='NONPOSITIVE_DENOMINATOR'
            else:
                with localcontext() as ctx:
                    ctx.prec = 50
                    value = cap / denominator.value
                status='REFERENCE';reason=qualification
        elif eligible and not attributable:
            reason='COMMON_INCOME_ATTRIBUTION_NOT_ESTABLISHED'
        if qualification == 'UNAVAILABLE':
            reason='CAPITAL_OR_PRICE_INPUT_UNAVAILABLE'
        metrics[key] = ValuationMetric(value=value, unit='multiple', status=status, reason=reason,
            evidence_ids=list(dict.fromkeys(parents+denominator.evidence_ids)), period=period, as_of=evaluated_at)
    source_ids=list(dict.fromkeys(eid for metric in metrics.values() for eid in metric.evidence_ids))
    assumption_text = [
        'No split, reverse split, stock dividend, issuance, buyback or conversion changes the reported common-share basis between share date and price date.',
        'Every reported common share has the same economic price as the input instrument; class conversion and multiple listing prices are not reconciled.',
    ]
    if assumptions.consolidated_income_available_to_common:
        assumption_text.append('Consolidated TTM net income is assumed entirely attributable to common equity; preferred/NCI allocation is not independently verified. This is equity value / TTM net income, not provider-standard P/E.')
    payload = {'calculation':'user_price_sec_reference_v1', 'calculation_version':2,
        'context':'DETACHED_LOCAL_WHAT_IF_NOT_ORIGINAL_RESEARCH_EVIDENCE',
        'task_id':task.task_id, 'revision':task.revision,
        'bound_task_projection_hash':content_hash(task.model_dump(mode='json')),
        'symbol':task.focus_symbol, 'qualification_status':qualification,
        'qualification_notes':qualification_notes,'listing_identity_source_ids':listing_source_ids,
        'price_as_of':request.price_date.isoformat(), 'price_time_precision':'DATE_ONLY',
        'calendar_guard':{'source_uri':CALENDAR_SOURCE,'coverage_years':sorted(CLOSED_DATES),
            'meaning':'Published full-closure/weekend guard only; actual trading, emergency halts, early-close time and user observation not independently verified'},
        'shares_as_of':share['end'] if share else None,
        'filing_available_at':share['available_at'] if share else None,
        'research_cutoff':task.cutoff.isoformat(), 'evaluated_at':evaluated_at.isoformat(),
        'currency':request.currency,'price_evidence':price_evidence.model_dump(mode='json'),
        'shares_reference':share,'ttm_period':period,
        'ttm_inputs':{k:report.metrics[k].model_dump(mode='json') for k in
            ('ttm_revenue','ttm_net_income','ttm_operating_cash_flow','ttm_diluted_eps')},
        'metrics':{k:v.model_dump(mode='json') for k,v in metrics.items()},
        'source_ids':source_ids,'assumptions':request.assumptions.model_dump(),
        'required_conditions':assumption_text,'rejected_inputs':reasons,
        'action_applicability':{'original_action_unchanged':True,'sent_to_deep':False,
            'note':'Local conditional/scenario arithmetic only. A multiple alone cannot support ADD. Recheck operating thesis, counterevidence and position applicability; original judgment has not consumed these inputs.'},
        'limitations':['No independently verified quote, current capitalization, capital action coverage or class reconciliation.',
            'Share count is a dated SEC instant, not current float or diluted weighted-average EPS shares.',
            'P/S uses whole-company revenue without debt/cash or preferred-claim adjustment; no EV, target, forecast or trade.',
            'User price/rights/assumptions are assertions, not verified facts. No model transfer, source refresh or saved Revision change.']}
    payload['scenario_difference'] = scenario_difference(payload, request.comparison_price)
    receipt_hash = content_hash(payload)
    receipt = EvidenceItem(evidence_id='E'+receipt_hash[7:23], evidence_type='calculation', provider='python_decimal',
        symbol=task.focus_symbol,title='日期化用户价格 × SEC · 条件参考／假设情景',
        published_at=evaluated_at, retrieved_at=evaluated_at, as_of=evaluated_at,
        content_hash=receipt_hash, structured_data=payload)
    return {'report':payload,'price_receipt':price_receipt.model_dump(mode='json'),
        'calculation_receipt':receipt.model_dump(mode='json'),'persisted':False,'external_calls':0}


def scenario_difference(report, comparison_price):
    """Two user inputs, identical frozen basis. No provider-price reuse or action."""
    if comparison_price is None:
        return None
    result = {'status':'UNAVAILABLE','comparison_price':str(comparison_price),
        'comparison_identity':'USER_DEFINED_SCENARIO_NOT_MARKET_OBSERVATION',
        'direction':'INPUT_MINUS_COMPARISON','metrics':{},
        'unchanged':['FROZEN_FINANCIAL_EVIDENCE','CAPITAL_ASSUMPTIONS','OPERATING_THESIS','ORIGINAL_ACTION','PORTFOLIO'],
        'meaning':'Price sensitivity only; not a market move, return, target price or new research judgment.'}
    if report['qualification_status'] == 'UNAVAILABLE':
        return result
    current = Decimal(report['price_evidence']['price'])
    shares = Decimal(report['shares_reference']['value'])
    with localcontext() as ctx:
        ctx.prec = 50
        result.update(status='ILLUSTRATIVE_SCENARIO',input_price=str(current),
            price_delta=str(current-comparison_price),
            price_delta_percent=str((current-comparison_price)/comparison_price*100),
            price_delta_percent_display=format((current-comparison_price)/comparison_price*100,'.2f'))
        for key,denom in [('reference_equity_value',None),('reference_ps','ttm_revenue'),
                          ('reference_pe','ttm_net_income')]:
            metric = report['metrics'][key]
            value = Decimal(metric['value']) if metric['value'] is not None else None
            base = None
            if value is not None:
                base = comparison_price*shares
                if denom:
                    base /= Decimal(report['ttm_inputs'][denom]['value'])
            delta = value-base if base is not None else None
            base_display = ValuationMetric(value=base,unit=metric['unit'],status=metric['status'],reason='ILLUSTRATIVE_SCENARIO')
            delta_display = ValuationMetric(value=delta,unit=metric['unit'],status=metric['status'],reason='ILLUSTRATIVE_SCENARIO')
            result['metrics'][key] = {'comparison_value':str(base) if base is not None else None,
                'input_value':metric['value'],'delta':str(value-base) if base is not None else None,
                'comparison_display_value':base_display.display_value,'comparison_display_approximate':base_display.display_approximate,
                'delta_display_value':delta_display.display_value,'delta_display_approximate':delta_display.display_approximate,
                'unit':metric['unit'],'status':metric['status'],'reason':metric['reason']}
    return result
