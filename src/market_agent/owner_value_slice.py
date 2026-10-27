"""Deterministic, read-only Owner holding value slice.

One selected Owner holding -> one eligible public SEC event -> one qualified
market price or an explicit UNKNOWN -> a prior Thesis or THESIS_NOT_AVAILABLE
-> an evidence-grounded next check -> one single-account hypothetical What-if.

This module is pure: it never opens a database, provider, file or socket. The
caller supplies an already verified ``PortfolioSnapshot`` plus captured source
records. It creates no orders, persists nothing and sends real holdings nowhere.

Missing, stale, mis-denominated or rights-restricted prices are never promoted
into a real valuation. Hypothetical inputs stay explicitly hypothetical.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .domain import FrozenModel, PortfolioSnapshot, utc
from .tdb import content_hash

SLICE_SCHEMA = 'owner-value-slice-v1'
MAX_HOLDING_AGE_DAYS = 7
MAX_QUOTE_AGE_DAYS = 3
VALUATION_RIGHTS = {'SERVER_VERIFIED_DERIVATION', 'OWNER_AUTHORIZED_LOCAL_ONLY'}
SEC_ACCEPTED_RIGHTS = 'PUBLIC_SEC_FILING_METADATA'
SEC_BODY_RIGHTS = 'PUBLIC_SEC_FILING_BODY'

# Deterministic, auditable event -> thesis check mapping. No model is involved.
EVENT_CHECK_RULES = {
    '5.02': {
        'topic': 'OFFICER_OR_DIRECTOR_CHANGE',
        'next_check': (
            'Capture the Item 5.02 filing body and identify the officer or director, '
            'the effective date and any change to the capital-return or digital-asset '
            'program named in the prior thesis. Captured metadata alone does not '
            'establish thesis impact.'
        ),
    },
}

# Substantive thesis concepts only. Governance words such as "board" or "officer"
# are deliberately excluded: they match almost any 8-K and manufacture relevance.
THESIS_ANCHOR_TERMS = {
    'SHARE_REPURCHASE': ('repurchase', 'buyback', 'buy-back'),
    'TOKENIZATION': ('tokeniz*', 'token asset'),
    'DIGITAL_ASSETS': ('digital asset', 'crypto'),
    'CAPITAL_RETURN': ('dividend', 'capital return'),
    'GUIDANCE': ('guidance', 'outlook'),
    'REGULATORY_ACTION': ('enforcement action', 'regulatory action'),
}

SYMBOL_PATTERN = r'^[A-Z][A-Z0-9.\-]{0,14}$'
ACCESSION_PATTERN = r'^\d{10}-\d{2}-\d{6}$'
HASH_PATTERN = r'^sha256:[0-9a-f]{64}$'
DOCUMENT_PATTERN = r'^SD[0-9a-f]{16}$'
_MONTH_DATE = (r'(?:January|February|March|April|May|June|July|August|September|'
               r'October|November|December)\s+\d{1,2},\s+\d{4}')
_PERSON = r"[A-Z][a-z]+(?: [A-Z][a-z.'\-]+){1,2}"
_ROLE = (r'(?:Chief [A-Z][A-Za-z ]{2,40}Officer|principal accounting officer|'
         r'Corporate Controller|Chief Financial Officer|PAO)')


def _exact(value, name):
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f'{name} must be an exact decimal string, never a float')
    if isinstance(value, str) and 'e' in value.lower():
        raise ValueError(f'{name} must use ordinary decimal notation')
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError(f'{name} must be finite')
    return number


def plain(value: Decimal) -> str:
    """Ordinary decimal text: never exponent form, never a signed zero."""
    return format(abs(value) if value == 0 else value, 'f')


class SliceRefused(ValueError):
    """Typed, public refusal. The message is a stable code, not private text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ThesisItem(FrozenModel):
    text: str = Field(min_length=1, max_length=2000)
    evidence_ids: tuple[str, ...] = ()


class ThesisRecord(FrozenModel):
    thesis_version_id: str = Field(min_length=1, max_length=100)
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    as_of: datetime
    status: str = Field(min_length=1, max_length=40)
    evidence_id: str = Field(min_length=1, max_length=100)
    # When the version and its cited evidence were actually stored. A backdated
    # as_of alone is not proof that the Owner could have known it earlier.
    stored_available_at: datetime | None = None
    core_drivers: tuple[ThesisItem, ...] = ()
    key_assumptions: tuple[ThesisItem, ...] = ()
    execution_risks: tuple[ThesisItem, ...] = ()
    invalidation_conditions: tuple[ThesisItem, ...] = ()

    _normalize_as_of = field_validator('as_of')(utc)
    _normalize_stored = field_validator('stored_available_at')(lambda v: utc(v) if v else None)


class SecEvent(FrozenModel):
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    cik: str = Field(pattern=r'^\d{10}$')
    accession: str = Field(pattern=ACCESSION_PATTERN)
    form: str = Field(min_length=1, max_length=20)
    items: tuple[str, ...] = ()
    accepted_at: datetime
    captured_at: datetime
    source_uri: str = Field(min_length=1, max_length=500)
    source_hash: str = Field(pattern=HASH_PATTERN)
    rights: str = Field(min_length=1, max_length=200)

    _normalize_accepted = field_validator('accepted_at')(utc)
    _normalize_captured = field_validator('captured_at')(utc)


class SecBodyChunk(FrozenModel):
    sequence: int = Field(ge=0)
    section: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=8000)
    content_hash: str = Field(pattern=HASH_PATTERN)


class SecFilingBody(FrozenModel):
    """The public filing body already captured by the Owner research store.

    Chunk hashes use the repository's canonical SEC chunk hashing, so any edit
    to the stored text is refused before the body can ground a thesis check.
    """
    accession: str = Field(pattern=ACCESSION_PATTERN)
    cik: str = Field(pattern=r'^\d{10}$')
    document_id: str = Field(pattern=DOCUMENT_PATTERN)
    form: str = Field(min_length=1, max_length=20)
    filed_at: datetime
    retrieved_at: datetime
    source_uri: str = Field(min_length=1, max_length=500)
    content_hash: str = Field(pattern=HASH_PATTERN)
    rights: Literal['PUBLIC_SEC_FILING_BODY']
    chunks: tuple[SecBodyChunk, ...] = Field(min_length=1)

    _normalize_filed = field_validator('filed_at')(utc)
    _normalize_retrieved = field_validator('retrieved_at')(utc)

    @model_validator(mode='after')
    def chunks_are_contiguous_and_authentic(self):
        for index, chunk in enumerate(self.chunks):
            if chunk.sequence != index:
                raise ValueError('SEC body chunks must be contiguous from sequence 0')
            expected = content_hash({
                'document_id': self.document_id,
                'sequence': chunk.sequence,
                'section': chunk.section,
                'text': chunk.text,
            })
            if expected != chunk.content_hash:
                raise ValueError(f'SEC body chunk {index} content_hash mismatch')
        return self

    def item_section(self, item: str) -> str | None:
        prefix = f'Item {item}'
        return next((chunk.section for chunk in self.chunks
                     if chunk.section.startswith(prefix)), None)

    def text(self) -> str:
        return ' '.join(chunk.text for chunk in self.chunks)


class PriceObservationInput(FrozenModel):
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    price: Decimal = Field(gt=0, le=Decimal('1000000000000'), max_digits=24, decimal_places=10)
    currency: str = Field(min_length=3, max_length=3)
    provider: str = Field(min_length=1, max_length=80)
    observed_at: datetime
    retrieved_at: datetime
    price_basis: Literal['UNADJUSTED', 'ADJUSTED', 'UNKNOWN']
    usage_rights: Literal['SERVER_VERIFIED_DERIVATION', 'OWNER_AUTHORIZED_LOCAL_ONLY',
                         'DISPLAY_ONLY', 'UNKNOWN']
    source_uri: str = Field(min_length=1, max_length=500)
    evidence_id: str = Field(min_length=1, max_length=100)
    delay_note: str = Field(default='UNKNOWN', max_length=200)

    _normalize_observed = field_validator('observed_at')(utc)
    _normalize_retrieved = field_validator('retrieved_at')(utc)

    @field_validator('price', mode='before')
    @classmethod
    def exact_price(cls, value):
        return _exact(value, 'price')

    @field_validator('currency')
    @classmethod
    def upper_currency(cls, value):
        return value.strip().upper()


class HoldingScope(FrozenModel):
    snapshot_id: str
    snapshot_content_hash: str = Field(pattern=HASH_PATTERN)
    snapshot_as_of: datetime
    source_kind: Literal['manual_entry', 'csv_import', 'capital_desk_v1_import']
    source_hash: str
    source_imported_at: datetime
    portfolio_id: str
    base_currency: str
    holding_age_days: int = Field(ge=0)
    holding_freshness: Literal['CURRENT', 'STALE']

    _normalize_as_of = field_validator('snapshot_as_of')(utc)
    _normalize_imported = field_validator('source_imported_at')(utc)


class OwnerHolding(FrozenModel):
    account_id: str
    account_label: str
    symbol: str
    quantity: Decimal
    avg_cost: Decimal
    currency: str
    user_stage: str
    as_of: datetime

    _normalize_as_of = field_validator('as_of')(utc)


class AccountScope(FrozenModel):
    account_id: str
    account_label: str
    cash: Decimal
    position_count: int = Field(ge=0)
    priced_position_count: int = Field(ge=0)
    same_symbol_other_accounts: tuple[str, ...] = ()


class EventQualification(FrozenModel):
    status: Literal['ELIGIBLE', 'EVENT_NOT_PROVIDED', 'EVENT_SYMBOL_MISMATCH',
                    'EVENT_FORM_NOT_ELIGIBLE', 'EVENT_ITEMS_EMPTY',
                    'PIT_INELIGIBLE_NOT_ACCEPTED_BY_EVALUATION',
                    'EVIDENCE_NOT_CAPTURED_BY_EVALUATION',
                    'CAPTURE_PRECEDES_ACCEPTANCE',
                    'RIGHTS_NOT_PUBLIC_FILING_METADATA']
    accession: str | None = None
    cik: str | None = None
    form: str | None = None
    items: tuple[str, ...] = ()
    accepted_at: datetime | None = None
    captured_at: datetime | None = None
    source_uri: str | None = None
    source_hash: str | None = None
    reasons: tuple[str, ...] = ()


class FilingBodyQualification(FrozenModel):
    status: Literal['BODY_VERIFIED', 'BODY_NOT_PROVIDED', 'BODY_EVENT_NOT_ELIGIBLE',
                    'BODY_ACCESSION_MISMATCH', 'BODY_ISSUER_MISMATCH',
                    'BODY_FORM_MISMATCH', 'BODY_RETRIEVED_AFTER_EVALUATION',
                    'BODY_FILED_AFTER_EVALUATION', 'BODY_RETRIEVED_BEFORE_FILING',
                    'BODY_ITEM_MISSING']
    accession: str | None = None
    document_id: str | None = None
    content_hash: str | None = None
    retrieved_at: datetime | None = None
    source_uri: str | None = None
    item_section: str | None = None
    chunk_count: int = Field(default=0, ge=0)
    reasons: tuple[str, ...] = ()


class InstrumentIdentity(FrozenModel):
    """Security identity read from the filing's own registered-securities table."""
    status: Literal['VERIFIED_FROM_FILING', 'UNVERIFIED', 'INSTRUMENT_SYMBOL_MISMATCH']
    symbol: str | None = None
    issuer_name: str | None = None
    security_title: str | None = None
    share_class: Literal['CLASS_A', 'CLASS_B', 'COMMON', 'UNKNOWN'] = 'UNKNOWN'
    par_value: Decimal | None = None
    exchange: str | None = None
    cik: str | None = None
    reasons: tuple[str, ...] = ()

    @field_validator('par_value', mode='before')
    @classmethod
    def exact_par(cls, value):
        return None if value is None else _exact(value, 'par value')


class OfficerChange(FrozenModel):
    """Deterministic Item 5.02 extraction. Nothing is inferred beyond the text."""
    status: Literal['PARSED', 'PARTIAL', 'UNPARSED']
    departing_name: str | None = None
    departing_role: str | None = None
    successor_name: str | None = None
    successor_role: str | None = None
    notice_date: str | None = None
    appointment_date: str | None = None
    effective_date: str | None = None
    evidence_chunk_hash: str | None = None
    excerpt: str
    reasons: tuple[str, ...] = ()


class PriceQualification(FrozenModel):
    status: Literal['QUALIFIED', 'UNAVAILABLE', 'SYMBOL_MISMATCH', 'CURRENCY_MISMATCH',
                    'PRICE_AFTER_EVALUATION', 'RETRIEVED_AFTER_EVALUATION',
                    'RETRIEVED_BEFORE_OBSERVED', 'STALE',
                    'RIGHTS_BLOCKED', 'PRICE_BASIS_UNKNOWN']
    valuation_eligible: bool
    price: Decimal | None = None
    currency: str | None = None
    provider: str | None = None
    observed_at: datetime | None = None
    retrieved_at: datetime | None = None
    price_basis: str | None = None
    usage_rights: str | None = None
    age_seconds: int | None = None
    max_age_days: int = MAX_QUOTE_AGE_DAYS
    source_uri: str | None = None
    evidence_id: str | None = None
    reasons: tuple[str, ...] = ()


class Valuation(FrozenModel):
    status: Literal['VALUED', 'UNKNOWN']
    basis: Literal['QUALIFIED_MARKET_PRICE_TIMES_AS_OF_QUANTITY', 'NONE']
    market_value: Decimal | None = None
    price: Decimal | None = None
    currency: str
    holding_as_of: datetime
    price_as_of: datetime | None = None
    current: bool = False
    reasons: tuple[str, ...] = ()


class MatchedThesisItem(FrozenModel):
    section: Literal['core_driver', 'key_assumption', 'execution_risk',
                     'invalidation_condition']
    text: str
    evidence_ids: tuple[str, ...]
    matched_terms: tuple[str, ...] = ()


class ThesisCheck(FrozenModel):
    status: Literal['THESIS_AVAILABLE', 'THESIS_NOT_AVAILABLE', 'THESIS_SYMBOL_MISMATCH',
                    'THESIS_AFTER_EVENT', 'THESIS_UNGROUNDED']
    thesis_version_id: str | None = None
    thesis_as_of: datetime | None = None
    thesis_status: str | None = None
    thesis_stored_available_at: datetime | None = None
    thesis_boundary: datetime | None = None
    owner_reviewed: bool = False
    matched_items: tuple[MatchedThesisItem, ...] = ()
    relevance: Literal['DIRECTLY_CHALLENGES_THESIS', 'RELATED_TO_THESIS',
                       'NOT_ADDRESSED', 'UNDETERMINED'] = 'UNDETERMINED'
    event_topics: tuple[str, ...] = ()
    thesis_anchors: tuple[str, ...] = ()
    anchor_hits_in_body: tuple[str, ...] = ()
    next_check: str | None = None
    next_check_status: Literal['READY', 'REQUIRES_DOCUMENT_BODY', 'REVIEW_BODY_ANCHOR',
                               'NO_THESIS_ANCHOR_IN_BODY', 'NO_PRIOR_THESIS',
                               'NOT_APPLICABLE'] = 'NOT_APPLICABLE'
    reasons: tuple[str, ...] = ()


class WhatIfRequest(FrozenModel):
    side: Literal['BUY', 'SELL']
    shares: Decimal = Field(gt=0, le=Decimal('1000000000'), max_digits=24, decimal_places=10)
    price_basis: Literal['QUALIFIED_MARKET', 'HYPOTHETICAL_OPERATOR_INPUT']
    price: Decimal | None = Field(default=None, gt=0, le=Decimal('1000000000000'),
                                  max_digits=24, decimal_places=10)
    fee: Decimal = Field(default=Decimal(0), ge=0, le=Decimal('1000000'),
                         max_digits=24, decimal_places=10)
    rationale: str = Field(min_length=1, max_length=400)

    @field_validator('shares', 'price', 'fee', mode='before')
    @classmethod
    def exact(cls, value):
        return value if value is None else _exact(value, 'what-if input')

    @field_validator('rationale')
    @classmethod
    def clean(cls, value):
        if not value.strip():
            raise ValueError('rationale is required')
        return value.strip()


class WhatIfReceipt(FrozenModel):
    mode: Literal['QUALIFIED_MARKET_SCENARIO', 'HYPOTHETICAL_SCENARIO']
    side: Literal['BUY', 'SELL']
    shares: Decimal
    scenario_price: Decimal
    price_basis: str
    fee: Decimal
    gross_value: Decimal
    cash_before: Decimal
    cash_after: Decimal
    cash_delta: Decimal
    shares_before: Decimal
    shares_after: Decimal
    focus_value_before: Decimal
    focus_value_after: Decimal
    focus_value_delta: Decimal
    nav_basis: Literal['QUALIFIED_MARKS', 'UNKNOWN']
    nav_before: Decimal | None = None
    nav_after: Decimal | None = None
    focus_weight_before: Decimal | None = None
    focus_weight_after: Decimal | None = None
    qualified_mark_count: int = Field(default=0, ge=0)
    unqualified_mark_count: int = Field(default=0, ge=0)
    unpriced_position_count: int = Field(ge=0)
    unpriced_symbols: tuple[str, ...] = ()
    rationale: str
    orders_created: int = Field(default=0, ge=0, le=0)
    persisted: Literal[False] = False
    external_send: Literal[False] = False
    limitations: tuple[str, ...] = ()


class SourceReceipt(FrozenModel):
    name: str
    source_kind: str
    source_ref: str
    observed_at: datetime
    content_hash: str
    rights: str

    _normalize_observed = field_validator('observed_at')(utc)


class OwnerValueSlice(FrozenModel):
    schema_version: Literal['owner-value-slice-v1'] = SLICE_SCHEMA
    generated_at: datetime
    redaction: Literal['LOCAL_FULL', 'SHAREABLE_REDACTED']
    holding_scope: HoldingScope
    holding: OwnerHolding
    account_scope: AccountScope
    event: EventQualification
    filing_body: FilingBodyQualification
    instrument: InstrumentIdentity
    officer_change: OfficerChange | None = None
    price: PriceQualification
    valuation: Valuation
    thesis_check: ThesisCheck
    whatif: WhatIfReceipt | None = None
    provenance: tuple[SourceReceipt, ...]
    limitations: tuple[str, ...]
    content_hash: str

    _normalize_generated = field_validator('generated_at')(utc)


def _scope(snapshot: PortfolioSnapshot, account_id: str, symbol: str,
           evaluation_time: datetime) -> tuple[HoldingScope, OwnerHolding, AccountScope]:
    if snapshot.as_of > evaluation_time:
        raise SliceRefused('SNAPSHOT_AFTER_EVALUATION')
    account = next((item for item in snapshot.accounts if item.account_id == account_id), None)
    if account is None:
        raise SliceRefused('ACCOUNT_NOT_FOUND')
    position = next((item for item in snapshot.positions
                     if item.account_id == account_id and item.symbol == symbol), None)
    if position is None:
        raise SliceRefused('SYMBOL_NOT_HELD_IN_ACCOUNT')
    others = tuple(sorted(item.account_id for item in snapshot.positions
                          if item.symbol == symbol and item.account_id != account_id))
    age_days = max(0, (evaluation_time - snapshot.as_of).days)
    holding_scope = HoldingScope(
        snapshot_id=snapshot.snapshot_id,
        snapshot_content_hash=snapshot.content_hash,
        snapshot_as_of=snapshot.as_of,
        source_kind=snapshot.source.kind,
        source_hash=snapshot.source.source_hash,
        source_imported_at=snapshot.source.imported_at,
        portfolio_id=snapshot.portfolio_id,
        base_currency=snapshot.base_currency,
        holding_age_days=age_days,
        holding_freshness='CURRENT' if age_days <= MAX_HOLDING_AGE_DAYS else 'STALE',
    )
    holding = OwnerHolding(
        account_id=account.account_id,
        account_label=account.name,
        symbol=position.symbol,
        quantity=position.quantity,
        avg_cost=position.avg_cost,
        currency=snapshot.base_currency,
        user_stage=position.user_stage,
        as_of=snapshot.as_of,
    )
    account_scope = AccountScope(
        account_id=account.account_id,
        account_label=account.name,
        cash=account.cash,
        position_count=sum(1 for item in snapshot.positions if item.account_id == account_id),
        priced_position_count=0,
        same_symbol_other_accounts=others,
    )
    return holding_scope, holding, account_scope


def qualify_event(event: SecEvent | None, holding: OwnerHolding,
                  evaluation_time: datetime) -> EventQualification:
    if event is None:
        return EventQualification(status='EVENT_NOT_PROVIDED',
                                  reasons=('NO_ELIGIBLE_SEC_EVENT_SUPPLIED',))
    common = dict(accession=event.accession, cik=event.cik, form=event.form,
                  items=event.items, accepted_at=event.accepted_at,
                  captured_at=event.captured_at, source_uri=event.source_uri,
                  source_hash=event.source_hash)
    if event.symbol != holding.symbol:
        return EventQualification(status='EVENT_SYMBOL_MISMATCH',
                                  reasons=('EVENT_SYMBOL_DIFFERS_FROM_HOLDING',), **common)
    if event.form not in {'8-K'}:
        return EventQualification(status='EVENT_FORM_NOT_ELIGIBLE',
                                  reasons=('ONLY_MATERIAL_8_K_EVENTS_ELIGIBLE',), **common)
    if not event.items:
        return EventQualification(status='EVENT_ITEMS_EMPTY',
                                  reasons=('8_K_ITEMS_REQUIRED',), **common)
    if event.accepted_at > evaluation_time:
        return EventQualification(status='PIT_INELIGIBLE_NOT_ACCEPTED_BY_EVALUATION',
                                  reasons=('ACCEPTED_AFTER_EVALUATION',), **common)
    if event.captured_at > evaluation_time:
        return EventQualification(status='EVIDENCE_NOT_CAPTURED_BY_EVALUATION',
                                  reasons=('CAPTURED_AFTER_EVALUATION',), **common)
    if event.captured_at < event.accepted_at:
        return EventQualification(status='CAPTURE_PRECEDES_ACCEPTANCE',
                                  reasons=('CAPTURE_CANNOT_PRECEDE_ACCEPTANCE',), **common)
    if event.rights != SEC_ACCEPTED_RIGHTS:
        return EventQualification(status='RIGHTS_NOT_PUBLIC_FILING_METADATA',
                                  reasons=('PUBLIC_FILING_METADATA_RIGHTS_REQUIRED',), **common)
    return EventQualification(status='ELIGIBLE', reasons=(), **common)


def qualify_price(observation: PriceObservationInput | None, holding: OwnerHolding,
                  evaluation_time: datetime) -> PriceQualification:
    if observation is None:
        return PriceQualification(status='UNAVAILABLE', valuation_eligible=False,
                                  reasons=('NO_AUTHORIZED_PRICE_SOURCE',))
    common = dict(price=observation.price, currency=observation.currency,
                  provider=observation.provider, observed_at=observation.observed_at,
                  retrieved_at=observation.retrieved_at, price_basis=observation.price_basis,
                  usage_rights=observation.usage_rights, source_uri=observation.source_uri,
                  evidence_id=observation.evidence_id)
    if observation.symbol != holding.symbol:
        return PriceQualification(status='SYMBOL_MISMATCH', valuation_eligible=False,
                                  reasons=('PRICE_SYMBOL_DIFFERS_FROM_HOLDING',), **common)
    if observation.currency != holding.currency:
        return PriceQualification(status='CURRENCY_MISMATCH', valuation_eligible=False,
                                  reasons=('NO_FX_CONVERSION_AUTHORIZED',), **common)
    if observation.observed_at > evaluation_time:
        return PriceQualification(status='PRICE_AFTER_EVALUATION', valuation_eligible=False,
                                  reasons=('PRICE_OBSERVED_AFTER_EVALUATION',), **common)
    if observation.retrieved_at > evaluation_time:
        return PriceQualification(status='RETRIEVED_AFTER_EVALUATION', valuation_eligible=False,
                                  reasons=('PRICE_RETRIEVED_AFTER_EVALUATION',), **common)
    if observation.retrieved_at < observation.observed_at:
        return PriceQualification(status='RETRIEVED_BEFORE_OBSERVED', valuation_eligible=False,
                                  reasons=('RETRIEVAL_CANNOT_PRECEDE_OBSERVATION',), **common)
    age = evaluation_time - observation.observed_at
    age_seconds = int(age.total_seconds())
    common['age_seconds'] = age_seconds
    if age > timedelta(days=MAX_QUOTE_AGE_DAYS):
        return PriceQualification(status='STALE', valuation_eligible=False,
                                  reasons=('QUOTE_AGE_EXCEEDED',), **common)
    if observation.usage_rights not in VALUATION_RIGHTS:
        return PriceQualification(status='RIGHTS_BLOCKED', valuation_eligible=False,
                                  reasons=('VALUATION_DERIVATION_RIGHTS_NOT_ADMITTED',), **common)
    if observation.price_basis != 'UNADJUSTED':
        return PriceQualification(status='PRICE_BASIS_UNKNOWN', valuation_eligible=False,
                                  reasons=('UNADJUSTED_PRICE_BASIS_REQUIRED',), **common)
    return PriceQualification(status='QUALIFIED', valuation_eligible=True,
                              reasons=(), **common)


def _items(thesis: ThesisRecord):
    for section, values in (('core_driver', thesis.core_drivers),
                            ('key_assumption', thesis.key_assumptions),
                            ('execution_risk', thesis.execution_risks),
                            ('invalidation_condition', thesis.invalidation_conditions)):
        for item in values:
            yield section, item


def _term_present(text: str, term: str) -> bool:
    if term.endswith('*'):
        return term[:-1] in text
    return re.search(rf'(?<![a-z]){re.escape(term)}(?![a-z])', text) is not None


def _anchors_in(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    return tuple(concept for concept, terms in THESIS_ANCHOR_TERMS.items()
                 if any(_term_present(lowered, term) for term in terms))


def _iso_date(text: str | None) -> str | None:
    if not text:
        return None
    try:
        return datetime.strptime(text, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def _role_after(text: str, anchor: str, window: int = 260) -> str | None:
    match = re.search(anchor, text)
    if match is None:
        return None
    found = re.search(_ROLE, text[match.end():match.end() + window])
    return found.group(0) if found else None


def qualify_filing_body(body: SecFilingBody | None, event: EventQualification,
                        evaluation_time: datetime) -> FilingBodyQualification:
    if event.status != 'ELIGIBLE':
        return FilingBodyQualification(status='BODY_EVENT_NOT_ELIGIBLE',
                                       reasons=('EVENT_NOT_ELIGIBLE_FOR_BODY_LINK',))
    if body is None:
        return FilingBodyQualification(status='BODY_NOT_PROVIDED',
                                       reasons=('FILING_BODY_NOT_CAPTURED',))
    common = dict(accession=body.accession, document_id=body.document_id,
                  content_hash=body.content_hash, retrieved_at=body.retrieved_at,
                  source_uri=body.source_uri, chunk_count=len(body.chunks))
    if body.accession != event.accession:
        return FilingBodyQualification(status='BODY_ACCESSION_MISMATCH',
                                       reasons=('BODY_ACCESSION_DIFFERS_FROM_EVENT',
                                               ), **common)
    if body.cik != event.cik:
        return FilingBodyQualification(status='BODY_ISSUER_MISMATCH',
                                       reasons=('BODY_CIK_DIFFERS_FROM_EVENT',), **common)
    if body.form != event.form:
        return FilingBodyQualification(status='BODY_FORM_MISMATCH',
                                       reasons=('BODY_FORM_DIFFERS_FROM_EVENT',), **common)
    if body.filed_at > evaluation_time:
        return FilingBodyQualification(status='BODY_FILED_AFTER_EVALUATION',
                                       reasons=('BODY_FILED_AFTER_EVALUATION',), **common)
    if body.retrieved_at > evaluation_time:
        return FilingBodyQualification(status='BODY_RETRIEVED_AFTER_EVALUATION',
                                       reasons=('BODY_RETRIEVED_AFTER_EVALUATION',),
                                       **common)
    if event.accepted_at is not None and body.retrieved_at < event.accepted_at:
        return FilingBodyQualification(status='BODY_RETRIEVED_BEFORE_FILING',
                                       reasons=('BODY_RETRIEVED_BEFORE_FILING',), **common)
    section = body.item_section('5.02')
    if section is None:
        return FilingBodyQualification(status='BODY_ITEM_MISSING',
                                       reasons=('ITEM_5_02_SECTION_NOT_FOUND',), **common)
    return FilingBodyQualification(status='BODY_VERIFIED', item_section=section,
                                   reasons=(), **common)


def parse_instrument(body: SecFilingBody | None,
                     holding: OwnerHolding) -> InstrumentIdentity:
    if body is None:
        return InstrumentIdentity(status='UNVERIFIED', symbol=holding.symbol,
                                  reasons=('NO_FILING_BODY_FOR_SECURITY_IDENTITY',))
    text = body.text()
    security = re.search(
        r'(?P<title>Class [A-Z] [A-Za-z ]{2,25}Stock|Common Stock)\s*,\s*'
        r'\$(?P<par>\d+\.\d{1,6})\s*Par Value\s*'
        r'(?P<symbol>[A-Z][A-Z0-9.\-]{0,14})\s+'
        r'(?P<exchange>The Nasdaq Stock Market LLC|New York Stock Exchange|'
        r'NYSE American|Nasdaq Capital Market)', text)
    issuer_marker = text.find('(Exact name of registrant')
    issuer_name = None
    if issuer_marker > 0:
        tail = re.split(r'(?<=\d{4})\s+', text[:issuer_marker])[-1].strip()
        if 2 <= len(tail) <= 80 and re.search(r'[A-Z][a-z]', tail) and not re.search(r'\d{4}', tail):
            issuer_name = tail
    if security is None:
        return InstrumentIdentity(status='UNVERIFIED', symbol=holding.symbol,
                                  issuer_name=issuer_name, cik=body.cik,
                                  reasons=('REGISTERED_SECURITY_TABLE_NOT_FOUND',))
    title = security.group('title')
    filing_symbol = security.group('symbol')
    share_class = ('CLASS_A' if title.startswith('Class A')
                   else 'CLASS_B' if title.startswith('Class B') else 'COMMON')
    common = dict(symbol=filing_symbol, issuer_name=issuer_name, security_title=title,
                  share_class=share_class, par_value=security.group('par'),
                  exchange=security.group('exchange'), cik=body.cik)
    if filing_symbol != holding.symbol:
        return InstrumentIdentity(status='INSTRUMENT_SYMBOL_MISMATCH',
                                  reasons=('FILING_SECURITY_SYMBOL_DIFFERS_FROM_HOLDING',),
                                  **common)
    return InstrumentIdentity(status='VERIFIED_FROM_FILING', reasons=(), **common)


def parse_officer_change(body: SecFilingBody | None, item: str = '5.02') -> OfficerChange | None:
    if body is None:
        return None
    section = body.item_section(item)
    if section is None:
        return None
    chunk = next(chunk for chunk in body.chunks if chunk.section == section)
    text = ' '.join(c.text for c in body.chunks if c.section == section)
    departing = re.search(rf'(?P<name>{_PERSON})\s+notified\b', text)
    successor = re.search(rf'\bappointed\s+(?P<name>{_PERSON})', text)
    notice = re.search(rf'\bon\s+(?P<date>{_MONTH_DATE}),\s+{_PERSON}\s+notified\b', text)
    appointment = re.search(
        rf'\bOn\s+(?P<date>{_MONTH_DATE}),\s+the board of directors\b', text)
    effective = re.search(
        rf'\beffective\s+(?:as of\s+)?(?:the Effective Date\s*,\s*)?(?P<date>{_MONTH_DATE})',
        text)
    successor_role = _role_after(text, r'to serve as\b')
    if successor_role == 'PAO' and re.search(
            r'principal accounting officer\s*\([^)]{0,40}PAO', text, re.IGNORECASE):
        successor_role = 'principal accounting officer'
    title = re.search(r'with the title\s+(?P<title>[A-Z][A-Za-z ]{2,40})', text)
    values = {
        'departing_name': departing.group('name') if departing else None,
        'departing_role': _role_after(text, r'intention to resign as\b'),
        'successor_name': successor.group('name') if successor else None,
        'successor_role': successor_role,
        'notice_date': _iso_date(notice.group('date') if notice else None),
        'appointment_date': _iso_date(appointment.group('date') if appointment else None),
        'effective_date': _iso_date(effective.group('date') if effective else None),
    }
    required = ('departing_name', 'departing_role', 'successor_name',
                'successor_role', 'effective_date')
    present = sum(1 for key in required if values[key])
    if present == len(required):
        status, reasons = 'PARSED', ()
    elif present >= 3:
        status, reasons = 'PARTIAL', ('SOME_ITEM_5_02_FIELDS_NOT_PARSED',)
    else:
        status, reasons = 'UNPARSED', ('NO_DETERMINISTIC_ITEM_5_02_EXTRACTION',)
    if title and values['successor_role']:
        values['successor_role'] = (
            f"{values['successor_role']} ({title.group('title').strip()})")
    return OfficerChange(status=status, evidence_chunk_hash=chunk.content_hash,
                         excerpt=_excerpt(text), reasons=reasons, **values)


def _excerpt(text: str, limit: int = 700) -> str:
    if len(text) <= limit:
        return text.strip()
    cut = text.rfind('. ', 0, limit)
    return text[:cut + 1] if cut >= limit // 2 else text[:limit].rstrip()


def _officer_summary(officer: OfficerChange | None) -> str:
    if officer is None or officer.status == 'UNPARSED':
        return 'the Item 5.02 section'
    parts = []
    if officer.departing_name:
        parts.append(f"departing {officer.departing_name}"
                     + (f" as {officer.departing_role}" if officer.departing_role else ''))
    if officer.successor_name:
        parts.append(f"successor {officer.successor_name}"
                     + (f" as {officer.successor_role}" if officer.successor_role else ''))
    if officer.effective_date:
        parts.append(f"effective {officer.effective_date}")
    return 'an officer transition (' + '; '.join(parts) + ')' if parts else 'the Item 5.02 section'


def _event_body_text(body: SecFilingBody, event: EventQualification) -> str:
    sections = [body.item_section(item) for item in event.items]
    texts = [' '.join(chunk.text for chunk in body.chunks if chunk.section == section)
             for section in sections if section is not None]
    return ' '.join(texts) if texts else body.text()


def thesis_boundary(event: EventQualification, evaluation_time: datetime) -> datetime:
    """The strict instant before which a thesis must already have existed.

    The earlier of the SEC acceptance instant and the evaluation instant, so a
    thesis written at the same second as the filing never counts as prior
    knowledge. When no event exists, the evaluation instant is the boundary: a
    thesis dated in the future is still not knowledge the Owner had.
    """
    boundary = utc(evaluation_time)
    accepted = event.accepted_at
    if accepted is not None and accepted < boundary:
        boundary = accepted
    return boundary


def check_thesis(thesis: ThesisRecord | None, holding: OwnerHolding,
                 event: EventQualification, *, evaluation_time: datetime,
                 body_check: FilingBodyQualification | None = None,
                 body: SecFilingBody | None = None,
                 officer: OfficerChange | None = None) -> ThesisCheck:
    boundary = thesis_boundary(event, evaluation_time)
    if thesis is None:
        return ThesisCheck(
            status='THESIS_NOT_AVAILABLE',
            thesis_boundary=boundary,
            next_check='Establish a baseline thesis with cited public evidence before judging this event.',
            next_check_status='NO_PRIOR_THESIS',
            reasons=('NO_PRIOR_THESIS_FOR_SYMBOL',),
        )
    audit = dict(thesis_version_id=thesis.thesis_version_id,
                 thesis_as_of=thesis.as_of, thesis_status=thesis.status,
                 thesis_stored_available_at=thesis.stored_available_at,
                 thesis_boundary=boundary)
    if thesis.symbol != holding.symbol:
        return ThesisCheck(status='THESIS_SYMBOL_MISMATCH',
                           reasons=('THESIS_SYMBOL_DIFFERS_FROM_HOLDING',), **audit)
    if not thesis.evidence_id or any(not item.evidence_ids for _, item in _items(thesis)):
        return ThesisCheck(status='THESIS_UNGROUNDED',
                           reasons=('EVERY_THESIS_ITEM_REQUIRES_EVIDENCE',), **audit)
    if not any(True for _ in _items(thesis)):
        return ThesisCheck(status='THESIS_UNGROUNDED',
                           reasons=('AT_LEAST_ONE_CITED_THESIS_ITEM_REQUIRED',), **audit)
    # Strict, not ``<=``: a version written at the boundary instant could already
    # know about the event.
    if thesis.as_of >= boundary:
        return ThesisCheck(status='THESIS_AFTER_EVENT',
                           reasons=('THESIS_MUST_STRICTLY_PREDATE_EVENT_AND_EVALUATION',),
                           **audit)
    if thesis.stored_available_at is None:
        return ThesisCheck(status='THESIS_NOT_AVAILABLE',
                           reasons=('THESIS_STORED_AVAILABILITY_UNKNOWN',), **audit)
    if thesis.stored_available_at >= boundary:
        # Backdated as_of, written after the event: not prior knowledge.
        return ThesisCheck(status='THESIS_NOT_AVAILABLE',
                           reasons=('THESIS_RECORDED_AFTER_BOUNDARY',), **audit)
    body_check = body_check or FilingBodyQualification(
        status='BODY_NOT_PROVIDED', reasons=('FILING_BODY_NOT_CAPTURED',))
    rules = [EVENT_CHECK_RULES[item] for item in event.items if item in EVENT_CHECK_RULES]
    topics = tuple(rule['topic'] for rule in rules)
    thesis_concepts = {concept for _, item in _items(thesis)
                       for concept in _anchors_in(item.text)}
    thesis_anchors = tuple(c for c in THESIS_ANCHOR_TERMS if c in thesis_concepts)
    common = dict(status='THESIS_AVAILABLE', owner_reviewed=False,
                  event_topics=topics, thesis_anchors=thesis_anchors, **audit)
    if not event.items or not rules:
        return ThesisCheck(relevance='UNDETERMINED', next_check=None,
                           next_check_status='NOT_APPLICABLE',
                           reasons=('THESIS_IS_NOT_OWNER_REVIEWED',
                                    'NO_EVENT_RULE_FOR_ITEMS'), **common)
    if body_check.status == 'BODY_VERIFIED' and body is not None:
        body_text = _event_body_text(body, event)
        lowered = body_text.lower()
        hits = _anchors_in(body_text)
        shared = tuple(concept for concept in hits if concept in thesis_concepts)
        if shared:
            matched = tuple(
                MatchedThesisItem(
                    section=section, text=item.text, evidence_ids=item.evidence_ids,
                    matched_terms=tuple(
                        term for concept in shared
                        for term in THESIS_ANCHOR_TERMS[concept]
                        if _term_present(lowered, term)))
                for section, item in _items(thesis)
                if set(_anchors_in(item.text)) & set(shared))
            next_check = (
                f"The captured Item 5.02 body mentions {', '.join(shared)}. Re-check "
                'whether this change alters those named drivers and cite the exact '
                'sentence; do not infer direction from the event type.')
            return ThesisCheck(
                matched_items=matched, relevance='RELATED_TO_THESIS',
                anchor_hits_in_body=hits, next_check=next_check,
                next_check_status='REVIEW_BODY_ANCHOR',
                reasons=('THESIS_IS_NOT_OWNER_REVIEWED',), **common)
        anchors_text = ', '.join(thesis_anchors) if thesis_anchors else 'any tracked anchor'
        next_check = (
            f'The captured Item 5.02 body covers {_officer_summary(officer)}. It does '
            f'not mention the prior thesis anchors {anchors_text}. Record '
            'NO_THESIS_IMPACT unless a second-order link is documented; a governance '
            'transition alone is not evidence about those drivers.')
        return ThesisCheck(
            relevance='NOT_ADDRESSED', anchor_hits_in_body=hits,
            next_check=next_check, next_check_status='NO_THESIS_ANCHOR_IN_BODY',
            reasons=('THESIS_IS_NOT_OWNER_REVIEWED',), **common)
    next_check = ' '.join(rule['next_check'] for rule in rules) or None
    return ThesisCheck(
        relevance='UNDETERMINED', next_check=next_check,
        next_check_status='REQUIRES_DOCUMENT_BODY' if next_check else 'NOT_APPLICABLE',
        reasons=('THESIS_IS_NOT_OWNER_REVIEWED',), **common)


def _scenario_price(request: WhatIfRequest, price: PriceQualification) -> Decimal:
    if request.price_basis == 'QUALIFIED_MARKET':
        if not price.valuation_eligible or price.price is None:
            raise SliceRefused('QUALIFIED_MARKET_PRICE_REQUIRED')
        if request.price is not None and request.price != price.price:
            raise SliceRefused('INPUT_PRICE_DIFFERS_FROM_QUALIFIED_PRICE')
        return price.price
    if request.price is None:
        raise SliceRefused('HYPOTHETICAL_PRICE_REQUIRED')
    return request.price


def _qualified_marks(snapshot: PortfolioSnapshot, holding: OwnerHolding,
                     evaluation_time: datetime,
                     other_prices: tuple[PriceObservationInput, ...]
                     ) -> tuple[dict[str, Decimal], tuple[str, ...]]:
    """Peer marks that passed every qualification rule; nothing else is a mark.

    A bare number is never evidence. Each observation must name a position held
    in this account and pass symbol, currency, age, basis and rights checks
    before it can enter NAV or a weight. Rejected observations are reported so
    the gap stays visible instead of silently shrinking the denominator.
    """
    positions = {item.symbol for item in snapshot.positions
                 if item.account_id == holding.account_id}
    marks: dict[str, Decimal] = {}
    rejected: list[str] = []
    seen: set[str] = set()
    for observation in other_prices:
        symbol = observation.symbol
        if symbol in seen:
            raise SliceRefused('DUPLICATE_MARK_OBSERVATION')
        seen.add(symbol)
        if symbol == holding.symbol:
            raise SliceRefused('OTHER_MARKS_MUST_EXCLUDE_FOCUS_SYMBOL')
        if symbol not in positions:
            raise SliceRefused('MARK_SYMBOL_NOT_HELD_IN_ACCOUNT')
        peer = holding.model_copy(update={'symbol': symbol,
                                          'currency': snapshot.base_currency})
        check = qualify_price(observation, peer, evaluation_time)
        if check.valuation_eligible and check.price is not None:
            marks[symbol] = check.price
        else:
            rejected.append(symbol)
    return marks, tuple(sorted(rejected))


def compute_whatif(snapshot: PortfolioSnapshot, holding: OwnerHolding,
                   price: PriceQualification, request: WhatIfRequest | None,
                   *, evaluation_time: datetime,
                   other_prices: tuple[PriceObservationInput, ...] = (),
                   redaction: str = 'SHAREABLE_REDACTED') -> WhatIfReceipt | None:
    if request is None:
        return None
    marks, rejected = _qualified_marks(snapshot, holding, evaluation_time,
                                       other_prices)
    return _whatif_receipt(snapshot, holding, price, request, marks, rejected,
                           redaction)


def _whatif_receipt(snapshot: PortfolioSnapshot, holding: OwnerHolding,
                    price: PriceQualification, request: WhatIfRequest,
                    marks: dict[str, Decimal], rejected: tuple[str, ...],
                    redaction: str) -> WhatIfReceipt:
    scenario = _scenario_price(request, price)
    account = next(item for item in snapshot.accounts if item.account_id == holding.account_id)
    positions = [item for item in snapshot.positions
                 if item.account_id == holding.account_id]
    quantities = {item.symbol: item.quantity for item in positions}
    delta = request.shares if request.side == 'BUY' else -request.shares
    if quantities[holding.symbol] + delta < 0:
        raise SliceRefused('OVERSELL_NOT_ALLOWED')
    with localcontext() as ctx:
        ctx.prec = 80
        gross = request.shares * scenario
        if request.side == 'BUY':
            cash_after = account.cash - gross - request.fee
            if cash_after < 0:
                raise SliceRefused('INSUFFICIENT_CASH_INCLUDING_FEES')
        else:
            cash_after = account.cash + gross - request.fee
            if cash_after < 0:
                raise SliceRefused('INSUFFICIENT_CASH_INCLUDING_FEES')
        after_quantities = {**quantities, holding.symbol: quantities[holding.symbol] + delta}

        def value(qty, mark):
            return qty * mark

        # Deal arithmetic uses the operator's scenario price. NAV and weight use
        # only qualified marks, so a hypothetical price never becomes a mark.
        before_value = value(quantities[holding.symbol], scenario)
        after_value = value(after_quantities[holding.symbol], scenario)
        nav_marks = dict(marks)
        if price.valuation_eligible and price.price is not None:
            nav_marks[holding.symbol] = price.price
        missing = sorted(symbol for symbol, qty in quantities.items()
                         if qty > 0 and symbol not in nav_marks)
        if missing:
            nav_before = nav_after = weight_before = weight_after = None
            nav_basis = 'UNKNOWN'
        else:
            equity_before = sum((value(q, nav_marks[s]) for s, q in quantities.items()),
                                Decimal(0))
            equity_after = sum((value(q, nav_marks[s]) for s, q in after_quantities.items()
                                if q > 0), Decimal(0))
            nav_before = account.cash + equity_before
            nav_after = cash_after + equity_after
            focus_mark = nav_marks[holding.symbol]
            weight_before = (value(quantities[holding.symbol], focus_mark) / nav_before
                             if nav_before else None)
            weight_after = (value(after_quantities[holding.symbol], focus_mark) / nav_after
                            if nav_after else None)
            nav_basis = 'QUALIFIED_MARKS'
        shown_missing = tuple(missing) if redaction == 'LOCAL_FULL' else ()
        return WhatIfReceipt(
            mode=('QUALIFIED_MARKET_SCENARIO'
                  if request.price_basis == 'QUALIFIED_MARKET' else 'HYPOTHETICAL_SCENARIO'),
            side=request.side,
            shares=request.shares,
            scenario_price=scenario,
            price_basis=('QUALIFIED_MARKET' if request.price_basis == 'QUALIFIED_MARKET'
                         else 'HYPOTHETICAL_OPERATOR_INPUT_NOT_A_QUOTE'),
            fee=request.fee,
            gross_value=gross,
            cash_before=account.cash,
            cash_after=cash_after,
            cash_delta=cash_after - account.cash,
            shares_before=quantities[holding.symbol],
            shares_after=after_quantities[holding.symbol],
            focus_value_before=before_value,
            focus_value_after=after_value,
            focus_value_delta=after_value - before_value,
            nav_basis=nav_basis,
            nav_before=nav_before,
            nav_after=nav_after,
            focus_weight_before=weight_before,
            focus_weight_after=weight_after,
            qualified_mark_count=(len(marks) + (1 if holding.symbol in nav_marks else 0)),
            unqualified_mark_count=len(rejected),
            unpriced_position_count=len(missing),
            unpriced_symbols=tuple(shown_missing),
            rationale=request.rationale,
            limitations=(
                'Single USD cash account; no leverage, shorting, tax lots, FX or execution.',
                'The scenario price is applied to the deal arithmetic; it is never a mark.',
                'NAV and weight stay unknown unless every account position has a qualified mark.',
                'No order, persistence or external send is created.',
            ),
        )


def build_owner_value_slice(*, snapshot: PortfolioSnapshot, account_id: str, symbol: str,
                            evaluation_time: datetime, event: SecEvent | None = None,
                            filing_body: SecFilingBody | None = None,
                            price: PriceObservationInput | None = None,
                            thesis: ThesisRecord | None = None,
                            whatif: WhatIfRequest | None = None,
                            other_prices: tuple[PriceObservationInput, ...] = (),
                            redaction: Literal['LOCAL_FULL', 'SHAREABLE_REDACTED']
                            = 'SHAREABLE_REDACTED') -> OwnerValueSlice:
    evaluation_time = utc(evaluation_time)
    holding_scope, holding, account_scope = _scope(snapshot, account_id, symbol,
                                                   evaluation_time)
    event_check = qualify_event(event, holding, evaluation_time)
    body_check = qualify_filing_body(filing_body, event_check, evaluation_time)
    identity = parse_instrument(filing_body, holding)
    officer = (parse_officer_change(filing_body, '5.02')
               if body_check.status == 'BODY_VERIFIED' and '5.02' in event_check.items
               else None)
    price_check = qualify_price(price, holding, evaluation_time)
    thesis_check = check_thesis(thesis, holding, event_check,
                                evaluation_time=evaluation_time,
                                body_check=body_check, body=filing_body,
                                officer=officer)
    peer_marks, rejected_marks = _qualified_marks(snapshot, holding, evaluation_time,
                                                  other_prices)
    qualified_marks = set(peer_marks)
    if price_check.valuation_eligible:
        qualified_marks.add(symbol)
    account_scope = account_scope.model_copy(update={
        'priced_position_count': sum(
            1 for p in snapshot.positions
            if p.account_id == account_id and p.symbol in qualified_marks),
    })
    identity_blocked = identity.status == 'INSTRUMENT_SYMBOL_MISMATCH'
    if price_check.valuation_eligible and not identity_blocked and price_check.price is not None:
        with localcontext() as ctx:
            ctx.prec = 80
            market_value = holding.quantity * price_check.price
        valuation = Valuation(
            status='VALUED',
            basis='QUALIFIED_MARKET_PRICE_TIMES_AS_OF_QUANTITY',
            market_value=market_value,
            price=price_check.price,
            currency=holding.currency,
            holding_as_of=holding.as_of,
            price_as_of=price_check.observed_at,
            current=holding_scope.holding_freshness == 'CURRENT',
            reasons=(() if holding_scope.holding_freshness == 'CURRENT'
                     else ('HOLDING_SNAPSHOT_IS_STALE',)),
        )
    else:
        reasons = []
        if not price_check.valuation_eligible:
            reasons.append(f'PRICE_{price_check.status}')
        if identity_blocked:
            reasons.append('INSTRUMENT_IDENTITY_MISMATCH')
        if holding_scope.holding_freshness != 'CURRENT':
            reasons.append('HOLDING_SNAPSHOT_IS_STALE')
        valuation = Valuation(
            status='UNKNOWN', basis='NONE', currency=holding.currency,
            holding_as_of=holding.as_of,
            price_as_of=price_check.observed_at,
            reasons=tuple(reasons),
        )
    whatif_receipt = (_whatif_receipt(snapshot, holding, price_check, whatif,
                                      peer_marks, rejected_marks, redaction)
                      if whatif is not None else None)
    provenance = [
        SourceReceipt(name='owner_holdings', source_kind=holding_scope.source_kind,
                      source_ref=f'snapshot:{holding_scope.snapshot_id}',
                      observed_at=holding_scope.snapshot_as_of,
                      content_hash=holding_scope.snapshot_content_hash,
                      rights='OWNER_LOCAL_READ_ONLY'),
    ]
    if event_check.accepted_at is not None:
        provenance.append(SourceReceipt(
            name='sec_event', source_kind='SEC_EDGAR_SUBMISSIONS',
            source_ref=f'{event_check.accession}:{event_check.form}',
            observed_at=event_check.captured_at,
            content_hash=event_check.source_hash or 'sha256:' + '0' * 64,
            rights=SEC_ACCEPTED_RIGHTS))
    if body_check.status == 'BODY_VERIFIED' and body_check.retrieved_at is not None:
        provenance.append(SourceReceipt(
            name='sec_filing_body', source_kind='SEC_EDGAR_PRIMARY_DOCUMENT',
            source_ref=body_check.document_id or 'UNKNOWN',
            observed_at=body_check.retrieved_at,
            content_hash=body_check.content_hash or 'sha256:' + '0' * 64,
            rights=SEC_BODY_RIGHTS))
    if price_check.observed_at is not None:
        provenance.append(SourceReceipt(
            name='market_price', source_kind=price_check.provider or 'UNKNOWN',
            source_ref=price_check.evidence_id or 'UNKNOWN',
            observed_at=price_check.observed_at,
            content_hash=content_hash(price_check.model_dump(mode='json')),
            rights=price_check.usage_rights or 'UNKNOWN'))
    if thesis_check.thesis_version_id is not None:
        provenance.append(SourceReceipt(
            name='prior_thesis', source_kind='AI_DRAFT_THESIS',
            source_ref=thesis_check.thesis_version_id,
            observed_at=thesis_check.thesis_as_of,
            content_hash=content_hash(thesis.model_dump(mode='json')) if thesis else
            'sha256:' + '0' * 64,
            rights='DERIVED_FROM_CITED_PUBLIC_EVIDENCE'))
    limitations = [
        'Read-only slice: no order, broker integration, persistence or external send.',
        'The holding is the Owner-entered snapshot as of its as_of; it is not a broker reconciliation.',
        'The price is admitted only inside its rights, currency, basis and age limits.',
        'The prior thesis is an AI draft unless separately Owner-reviewed.',
        'The What-if is arithmetic on operator inputs, not investment advice or a forecast.',
        'SEC accepted_at, capture time and PIT eligibility are distinct fields.',
        'The filing body is quoted from the captured SEC primary document; chunk hashes are recomputed from the stored text, the document hash is the capture record.',
        'A governance event without a thesis anchor hit is a documented gap, not a clearance to trade.',
        'This receipt itself stays Owner-local: share only the to_ui_view projection, '
        'which withholds private fields under SHAREABLE_REDACTED.',
    ]
    receipt = {
        'schema_version': SLICE_SCHEMA,
        'generated_at': evaluation_time.isoformat(),
        'redaction': redaction,
        'holding_scope': holding_scope.model_dump(mode='json'),
        'holding': holding.model_dump(mode='json'),
        'account_scope': account_scope.model_dump(mode='json'),
        'event': event_check.model_dump(mode='json'),
        'filing_body': body_check.model_dump(mode='json'),
        'instrument': identity.model_dump(mode='json'),
        'officer_change': officer.model_dump(mode='json') if officer else None,
        'price': price_check.model_dump(mode='json'),
        'valuation': valuation.model_dump(mode='json'),
        'thesis_check': thesis_check.model_dump(mode='json'),
        'whatif': whatif_receipt.model_dump(mode='json') if whatif_receipt else None,
        'provenance': [item.model_dump(mode='json') for item in provenance],
        'limitations': limitations,
    }
    base = OwnerValueSlice(**receipt, content_hash='sha256:' + '0' * 64)
    recomputed = content_hash(base.model_dump(mode='json', exclude={'content_hash'}))
    return base.model_copy(update={'content_hash': recomputed})
