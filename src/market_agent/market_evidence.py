"""Read-time market identity; never rehash or rewrite stored evidence receipts.

New York calendar dates are publication labels, not proof of a trading day or
the time an underlying event happened. Quote sessions require provider fields.
"""
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator

from .domain import EvidenceItem, FrozenModel, utc

NEW_YORK = ZoneInfo('America/New_York')
NEWS_MAX_AGE = timedelta(days=7)
FILING_MAX_AGE = timedelta(days=120)


class MarketEvidenceIdentity(FrozenModel):
    symbol: str | None
    provider: str
    source_url: str | None
    evidence_id: str
    source_type: str
    published_at: datetime | None
    market_as_of: datetime | None
    retrieved_at: datetime
    trade_session: str = 'UNKNOWN'
    data_latency_seconds: int | None = Field(default=None, ge=0)
    latency_basis: Literal['RETRIEVAL_MINUS_SOURCE_TIMESTAMP'] = 'RETRIEVAL_MINUS_SOURCE_TIMESTAMP'
    feed_delay_status: Literal['UNKNOWN'] = 'UNKNOWN'
    freshness_status: Literal['TODAY', 'RECENT', 'HISTORICAL', 'UNKNOWN', 'INELIGIBLE']
    market_timezone: Literal['America/New_York'] = 'America/New_York'
    publication_precision: str
    event_time_verified: bool = False

    _retrieved = field_validator('retrieved_at')(utc)


def market_identity(evidence: EvidenceItem, cutoff: datetime) -> MarketEvidenceIdentity:
    cutoff = utc(cutoff)
    data = evidence.structured_data or {}
    published = evidence.published_at
    precision = data.get('published_at_precision', 'INSTANT' if evidence.provider in
        {'finnhub', 'synthetic', 'synthetic-copy', 'synthetic-research'} else 'UNKNOWN')
    timestamp = evidence.as_of if evidence.evidence_type == 'market_data' else published
    status = 'UNKNOWN'
    if timestamp and (timestamp > cutoff or evidence.retrieved_at < timestamp):
        status = 'INELIGIBLE'
    elif timestamp and (evidence.evidence_type == 'market_data' or precision == 'INSTANT'):
        age = cutoff-timestamp
        if evidence.evidence_type == 'market_data':
            status = 'RECENT' if age <= timedelta(minutes=30) else 'HISTORICAL'
        elif age > NEWS_MAX_AGE:
            status = 'HISTORICAL'
        elif timestamp.astimezone(NEW_YORK).date() == cutoff.astimezone(NEW_YORK).date():
            status = 'TODAY'
        else:
            status = 'RECENT'
    return MarketEvidenceIdentity(symbol=evidence.symbol, provider=evidence.provider,
        source_url=evidence.source_uri, evidence_id=evidence.evidence_id,
        source_type=evidence.evidence_type, published_at=published,
        market_as_of=evidence.as_of, retrieved_at=evidence.retrieved_at,
        trade_session=str(data.get('session') or 'UNKNOWN'),
        data_latency_seconds=max(0, int((evidence.retrieved_at-timestamp).total_seconds())) if timestamp else None,
        freshness_status=status, publication_precision=precision)


def company_freshness(evidence: EvidenceItem, cutoff: datetime) -> str:
    if not evidence.published_at:
        return 'STALE'
    maximum = NEWS_MAX_AGE if evidence.evidence_type == 'news' else FILING_MAX_AGE
    return 'STALE' if utc(cutoff)-evidence.published_at > maximum else 'CURRENT'


def attention_freshness(row, cutoff):
    # Only the representative NEW receipt can refresh an event. A grouped alias
    # or old receipt is not evidence that the event occurred again today.
    evidence = EvidenceItem.model_validate(row['item']['packet']['evidence'][0])
    return market_identity(evidence, cutoff).model_dump(mode='json')
