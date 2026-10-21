"""Qualified, detached daily OHLCV zones; deterministic, never a trading signal."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, localcontext, ROUND_FLOOR, ROUND_CEILING
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from .dated_valuation import CLOSED_DATES, CALENDAR_SOURCE
from .domain import FrozenModel, utc
from .tdb import content_hash

NY=ZoneInfo('America/New_York')
# Published NYSE equity early closes; unsupported years refuse below.
EARLY_CLOSES={'2026-11-27','2026-12-24','2027-11-26','2028-07-03','2028-11-24'}
Price=Annotated[Decimal,Field(gt=0,max_digits=24,decimal_places=10,le=Decimal('1e12'))]


def close_time(day):
    if day.year not in CLOSED_DATES:
        raise ValueError('CALENDAR_YEAR_NOT_SUPPORTED')
    if day.weekday()>=5 or day.strftime('%m-%d') in CLOSED_DATES[day.year]:
        return None
    return datetime.combine(day,time(13 if day.isoformat() in EARLY_CLOSES else 16),NY).astimezone(timezone.utc)


class DailyBar(FrozenModel):
    session_date: date
    available_at: datetime
    open: Price
    high: Price
    low: Price
    close: Price
    volume: int = Field(ge=0,le=10**15,strict=True)
    _utc=field_validator('available_at')(utc)

    @field_validator('open','high','low','close',mode='before')
    @classmethod
    def exact(cls,value):
        if isinstance(value,(float,bool)):
            raise ValueError('Use exact decimal price strings')
        return value

    @model_validator(mode='after')
    def valid_ohlc(self):
        if not self.low<=self.open<=self.high or not self.low<=self.close<=self.high:
            raise ValueError('OHLC must lie inside low/high')
        return self


class OHLCVSeries(FrozenModel):
    symbol: str = Field(pattern=r'^[A-Z][A-Z0-9.\-]{0,14}$')
    currency: Literal['USD'] = 'USD'
    identity: Literal['SYNTHETIC','REAL','USER_ASSERTION','UNKNOWN']
    source_id: str = Field(min_length=1,max_length=160)
    source_note: str = Field(min_length=1,max_length=400)
    usage_rights: Literal['SYNTHETIC_OWNED','UNKNOWN','DISPLAY_ONLY']
    retrieved_at: datetime
    session: Literal['REGULAR','EXTENDED','UNKNOWN']
    adjustment: Literal['UNADJUSTED','SPLIT_ADJUSTED','UNKNOWN']
    adjustment_as_of: datetime
    adjustment_lineage: str = Field(max_length=400)
    split_status: Literal['NONE_CONFIRMED','CHANGE_DETECTED','UNKNOWN']
    halt_status: Literal['NONE_CONFIRMED','HALTED','UNKNOWN']
    share_class: Literal['SINGLE_COMMON','MULTI_CLASS','UNKNOWN']
    bars: tuple[DailyBar,...] = Field(min_length=1,max_length=252)
    _times=field_validator('retrieved_at','adjustment_as_of')(utc)


class TechnicalRequest(FrozenModel):
    revision: int = Field(ge=1)
    series: OHLCVSeries


def technical_zones(task,request):
    series=request.series;cutoff=task.cutoff
    def unavailable(code):
        return {'status':'UNAVAILABLE','reason':code,'zones':None,'live_data_status':'LIVE_DATA_NOT_VERIFIED',
            'task_id':task.task_id,'revision':task.revision,'external_calls':0,'persisted':False}
    if task.status!='COMPLETE' or not task.action_decision or request.revision!=task.revision or series.symbol!=task.focus_symbol:
        return unavailable('SAVED_ACTION_SYMBOL_REVISION_REQUIRED')
    if series.identity!='SYNTHETIC' or series.usage_rights!='SYNTHETIC_OWNED':
        return unavailable('REAL_SOURCE_RIGHTS_ADAPTER_NOT_ADMITTED')
    if series.session!='REGULAR':return unavailable('REGULAR_SESSION_REQUIRED')
    if series.share_class!='SINGLE_COMMON':return unavailable('SHARE_CLASS_UNRECONCILED')
    if series.split_status!='NONE_CONFIRMED':return unavailable('SPLIT_BASIS_UNRECONCILED')
    if series.halt_status!='NONE_CONFIRMED':return unavailable('HALT_STATUS_UNQUALIFIED')
    if series.adjustment=='UNKNOWN' or not series.adjustment_lineage.strip():return unavailable('ADJUSTMENT_LINEAGE_REQUIRED')
    if max(series.retrieved_at,series.adjustment_as_of)>cutoff:return unavailable('POST_CUTOFF_VINTAGE')
    if series.adjustment_as_of>series.retrieved_at:return unavailable('ADJUSTMENT_AFTER_RETRIEVAL')
    for source in task.sources:
        if source.evidence.symbol!=series.symbol:continue
        data=source.evidence.structured_data or {};check=data.get('capital_basis_check') or {}
        listing=data.get('issuer_listing_identity') or {}
        if (data.get('listed_common_symbols') not in (None,[series.symbol])
                or isinstance(listing,dict) and listing.get('tickers') not in (None,[series.symbol])
                or isinstance(check,dict) and (check.get('split_status')=='CHANGE_DETECTED'
                    or check.get('share_count_status')=='CHANGE_DETECTED'
                    or check.get('share_class_alignment')=='MISMATCH')):
            return unavailable('SAVED_EVIDENCE_CAPITAL_CONFLICT')
    bars=series.bars
    if len(bars)<21:return unavailable('AT_LEAST_21_COMPLETED_BARS_REQUIRED')
    days=[b.session_date for b in bars]
    if days!=sorted(set(days)):return unavailable('DUPLICATE_OR_UNSORTED_SESSIONS')
    if (days[-1]-days[0]).days>400:return unavailable('WINDOW_TOO_LONG')
    try:
        for bar in bars:
            closed=close_time(bar.session_date)
            if closed is None:return unavailable('NON_TRADING_SESSION')
            if not closed<=bar.available_at<=series.retrieved_at<=cutoff:return unavailable('INCOMPLETE_OR_POST_CUTOFF_BAR')
            if bar.volume==0:return unavailable('HALTED_OR_ZERO_VOLUME_BAR')
        day=days[0];expected=[]
        while day<=days[-1]:
            if close_time(day) is not None:expected.append(day)
            day+=timedelta(days=1)
        if days!=expected:return unavailable('MISSING_TRADING_SESSION')
        latest=cutoff.astimezone(NY).date()
        while close_time(latest) is None or close_time(latest)>cutoff:
            latest-=timedelta(days=1)
        if days[-1]!=latest:return unavailable('STALE_OR_FUTURE_SERIES')
        next_day=latest+timedelta(days=1)
        while close_time(next_day) is None:next_day+=timedelta(days=1)
        valid_until=close_time(next_day)
    except ValueError:return unavailable('CALENDAR_YEAR_NOT_SUPPORTED')
    # Neither an asserted split flag nor an adjustment label proves continuity.
    # A >=15% price discontinuity is a review trigger, NOT a split classifier.
    # Legitimate news gaps can also refuse; never repair/rescale bars heuristically.
    with localcontext() as context:
        context.prec=64
        for previous,current in zip(bars,bars[1:]):
            for field in ('open','close'):
                if abs(getattr(current,field)-previous.close)*100 >= previous.close*15:
                    return unavailable('PRICE_DISCONTINUITY_REQUIRES_RECONCILIATION')
        sma=sum((b.close for b in bars[-20:]),Decimal(0))/20
        tr=[max(b.high-b.low,abs(b.high-bars[i-1].close),abs(b.low-bars[i-1].close)) for i,b in enumerate(bars) if i]
        atr=sum(tr[-14:],Decimal(0))/14  # Simple mean, explicitly not Wilder smoothing.
        low=min(b.low for b in bars[-10:]);high=max(b.high for b in bars[-10:])
        if atr==0 or low+atr/2>=high-atr/2:return unavailable('RANGE_NOT_SEPARABLE')
        def bound(value,rounding):return str(value.quantize(Decimal('0.0000000001'),rounding=rounding))
        support=[bound(low,ROUND_FLOOR),bound(low+atr/2,ROUND_CEILING)]
        resistance=[bound(high-atr/2,ROUND_FLOOR),bound(high,ROUND_CEILING)]
        if Decimal(support[0])<=0:return unavailable('PRICE_SCALE_BELOW_RESOLUTION')
        if Decimal(support[1])>=Decimal(resistance[0]):return unavailable('ROUNDED_RANGE_NOT_SEPARABLE')
        value={'status':'AVAILABLE_SYNTHETIC','live_data_status':'LIVE_DATA_NOT_VERIFIED',
            'task_id':task.task_id,'revision':task.revision,'symbol':series.symbol,
            'bound_task_hash':content_hash(task.model_dump(mode='json')),
            'series_hash':content_hash(series.model_dump(mode='json')),'series':series.model_dump(mode='json'),
            'research_cutoff':cutoff.isoformat(),'last_session':days[-1].isoformat(),
            'valid_until':valid_until.isoformat(),'continuity_check':{'algorithm':'ADJACENT_OPEN_CLOSE_VS_PREVIOUS_CLOSE_V1','review_threshold_percent':'15','meaning':'Review trigger only, not proof of no corporate actions'},'calendar_source':CALENDAR_SOURCE,'calendar_years':[2026,2027,2028],
            'algorithm':{'version':'daily-range-atr-v1','sma_sessions':20,'atr_sessions':14,
                'atr_method':'SIMPLE_MEAN_TRUE_RANGE_NOT_WILDER','range_sessions':10,'band_atr_fraction':'0.5',
                'decimal_precision':64},
            'last_close':str(bars[-1].close),'sma20':str(sma),'atr14':str(atr),
            'zones':{'support':support,'resistance':resistance},
            'zone_rounding':'OUTWARD_10_DECIMAL_PLACES_NOT_EXECUTION_TICK',
            'trend':'ABOVE_SMA20' if bars[-1].close>sma else 'BELOW_SMA20' if bars[-1].close<sma else 'AT_SMA20',
            'invalidations':['Next completed regular session requires recomputation.',
                'A close below support lower bound or above resistance upper bound invalidates this historical range.',
                'Any split, halt, share-class/calendar/vintage change requires new qualified evidence.'],
            'original_action':task.action_decision.decision_action,'action_changed':False,
            'external_calls':0,'persisted':False,
            'limitations':['Synthetic OHLCV only; real provider rights, vintage and halt/action coverage NOT_VERIFIED.',
                'Historical candidate bands are not valuation, target price, forecast or an instruction to trade.',
                'Same adjustment basis across all OHLC; no automatic split repair, FX or LLM-generated levels.',
                'Continuity guard can miss smaller/distributed corporate actions; legitimate news gaps can refuse. T1 remains blocked.',
                'Calendar covers published 2026–2028 sessions; emergency closures are not independently verified.']}
        return {**value,'content_hash':content_hash(value)}
