"""Minimal, in-process task memory for follow-up questions.

This is not a memory platform. It is a bounded frozen record set (at most
`max_records`) that exists only to keep one research thread coherent: which
symbol, goal and account scope a follow-up inherits, which prior thesis has
provenance, and which earlier material is stale or out of scope. It performs no
I/O, no embedding lookup, no external store and no model call.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .adaptive_intent import Goal, PriorTaskSummary
from .domain import FrozenModel, utc
from .research_task import Objective, extract_literals

MemoryKind = Literal['THESIS', 'DECISION', 'ANSWER', 'USER_PREFERENCE',
                     'USER_PRICE_ASSERTION', 'EVIDENCE', 'PORTFOLIO_REF']
MemoryIdentity = Literal['OWNER_CONFIRMED_THESIS', 'AI_DRAFT', 'HISTORICAL_DECISION',
                         'USER_ASSERTION', 'USER_PREFERENCE', 'MARKET_FACT']
MemoryScopeKind = Literal['ACCOUNT_BOUND', 'ACCOUNT_AGNOSTIC']
ExclusionReason = Literal['SCOPE_MISMATCH', 'NOT_YET_AVAILABLE', 'STALE',
                          'SUPERSEDED', 'UNVERIFIED']

_ACCOUNT_BOUND_KINDS = frozenset({'PORTFOLIO_REF'})

_THESIS_PRIORITY = {'OWNER_CONFIRMED_THESIS': 3, 'AI_DRAFT': 2, 'HISTORICAL_DECISION': 1}
_IDENTITY_PRIORITY = {**_THESIS_PRIORITY, 'MARKET_FACT': 4, 'USER_ASSERTION': 1,
                      'USER_PREFERENCE': 1}
_ALLOWED_IDENTITIES: dict[MemoryKind, set[str]] = {
    'THESIS': {'OWNER_CONFIRMED_THESIS', 'AI_DRAFT'},
    'DECISION': {'HISTORICAL_DECISION'},
    'ANSWER': {'AI_DRAFT'},
    'USER_PREFERENCE': {'USER_PREFERENCE'},
    'USER_PRICE_ASSERTION': {'USER_ASSERTION'},
    'EVIDENCE': {'MARKET_FACT'},
    'PORTFOLIO_REF': {'MARKET_FACT'},
}


class MemoryRecord(FrozenModel):
    record_id: str = Field(min_length=1, max_length=80)
    kind: MemoryKind
    identity: MemoryIdentity
    symbol: str | None = None
    account_ids: tuple[str, ...] = ()
    scope_kind: MemoryScopeKind = 'ACCOUNT_AGNOSTIC'
    as_of: datetime
    summary: str = Field(min_length=1, max_length=400)
    verified: bool = False
    numeric_value: Decimal | None = None
    origin_question: str | None = Field(default=None, max_length=1200)
    effective_cutoff: datetime | None = None
    pit_ok: bool = True
    stale: bool = False
    superseded_by: str | None = None

    _as_of = field_validator('as_of')(utc)
    _cutoff = field_validator('effective_cutoff')(
        lambda value: None if value is None else utc(value))

    @field_validator('symbol')
    @classmethod
    def _symbol(cls, value):
        return None if value is None else value.removeprefix('$').upper()

    @model_validator(mode='before')
    @classmethod
    def _default_scope_kind(cls, data):
        """A portfolio reference is derived from accounts; nothing else defaults to it."""

        if isinstance(data, dict) and data.get('scope_kind') is None:
            data = dict(data)
            data['scope_kind'] = ('ACCOUNT_BOUND' if data.get('kind') in _ACCOUNT_BOUND_KINDS
                                  else 'ACCOUNT_AGNOSTIC')
        return data

    @model_validator(mode='after')
    def _provenance(self):
        if self.kind in _ACCOUNT_BOUND_KINDS and not self.account_bound:
            raise ValueError('a portfolio reference is always account-bound')
        if self.identity not in _ALLOWED_IDENTITIES[self.kind]:
            raise ValueError(f'{self.kind} cannot carry identity {self.identity}')
        if self.identity in {'USER_ASSERTION', 'USER_PREFERENCE', 'AI_DRAFT'} and self.verified:
            raise ValueError(f'{self.identity} can never be verified')
        if self.identity == 'MARKET_FACT' and not self.verified:
            raise ValueError('a market fact requires verification')
        if self.kind == 'USER_PRICE_ASSERTION' and self.numeric_value is None:
            raise ValueError('a price assertion needs its literal value')
        if self.numeric_value is not None and (not self.numeric_value.is_finite()
                                               or self.numeric_value <= 0):
            raise ValueError('memory numeric value must be finite and positive')
        return self

    @property
    def market_fact(self) -> bool:
        return self.identity == 'MARKET_FACT' and self.verified

    @property
    def account_bound(self) -> bool:
        return self.scope_kind == 'ACCOUNT_BOUND'


def is_market_fact(record: MemoryRecord) -> bool:
    """User assertions, preferences and drafts are never market observations."""

    return record.market_fact


class TaskMemory(FrozenModel):
    version: Literal['task-memory-v1'] = 'task-memory-v1'
    thread_root_id: str = Field(min_length=1, max_length=80)
    current_task_id: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=1200)
    goal: Goal
    objective: Objective
    symbol: str | None = None
    account_ids: tuple[str, ...] = ()
    cutoff: datetime
    records: tuple[MemoryRecord, ...] = ()
    max_records: int = Field(default=12, ge=1, le=24)

    _cutoff = field_validator('cutoff')(utc)

    @field_validator('symbol')
    @classmethod
    def _symbol(cls, value):
        return None if value is None else value.removeprefix('$').upper()

    @model_validator(mode='after')
    def _bounded(self):
        if len(self.records) > self.max_records:
            raise ValueError('task memory exceeds its record bound')
        ids = [r.record_id for r in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate memory record')
        return self

    def prior(self) -> PriorTaskSummary:
        return PriorTaskSummary(task_id=self.current_task_id, thread_root_id=self.thread_root_id,
                                goal=self.goal, objective=self.objective, symbol=self.symbol,
                                account_ids=self.account_ids, cutoff=self.cutoff)

    def with_record(self, record: MemoryRecord) -> 'TaskMemory':
        records = [r for r in self.records if r.record_id != record.record_id]
        records.append(record)
        while len(records) > self.max_records:
            victim = min((r for r in records if r.record_id != record.record_id),
                         key=lambda r: (_IDENTITY_PRIORITY.get(r.identity, 0), r.as_of))
            records.remove(victim)
        return self.model_copy(update={'records': tuple(records)})


def _available_at(record: MemoryRecord) -> datetime:
    return record.effective_cutoff or record.as_of


def select_thesis(memory: TaskMemory, *, symbol: str | None = None,
                  cutoff: datetime | None = None) -> MemoryRecord | None:
    """Confirmed thesis outranks a draft; a draft never becomes the truth."""

    scope = (symbol or memory.symbol or '').upper() or None
    at = utc(cutoff) if cutoff is not None else memory.cutoff
    candidates = [r for r in memory.records
                  if r.kind == 'THESIS' and not r.superseded_by and r.pit_ok
                  and (scope is None or r.symbol == scope)
                  and r.as_of <= at and _available_at(r) <= at]
    if not candidates:
        return None
    return max(candidates, key=lambda r: (_THESIS_PRIORITY.get(r.identity, 0), r.as_of))


def select_user_price(memory: TaskMemory, *, symbol: str | None = None) -> MemoryRecord | None:
    scope = (symbol or memory.symbol or '').upper() or None
    return next((r for r in reversed(memory.records)
                 if r.kind == 'USER_PRICE_ASSERTION' and not r.stale
                 and (scope is None or r.symbol == scope)), None)


def assertion_literal_is_unchanged(record: MemoryRecord) -> bool:
    """Re-derive the literal from the original wording before reuse."""

    if record.origin_question is None or record.numeric_value is None:
        return False
    try:
        literals = extract_literals(record.origin_question)
    except ValueError:
        return False
    if len(literals['prices']) != 1:
        return False
    if Decimal(literals['prices'][0]) != record.numeric_value:
        return False
    if literals['symbols'] and record.symbol and literals['symbols'] != [record.symbol]:
        return False
    return True


class MemoryPartition(FrozenModel):
    fresh: tuple[str, ...] = ()
    historical: tuple[str, ...] = ()
    stale_requiring_revalidation: tuple[str, ...] = ()
    excluded: tuple[tuple[str, ExclusionReason], ...] = ()
    notes: tuple[str, ...] = ()

    def excluded_ids(self) -> set[str]:
        return {record_id for record_id, _ in self.excluded}


def _accounts_compatible(record: MemoryRecord, context_ids) -> bool:
    """Explicit ids narrow within the context; an empty id set is not automatically global.

    A symbol or market fact that was never derived from a portfolio travels
    freely. An account-bound record with no ids is an ALL_ACCOUNTS aggregate:
    it stays usable only under an aggregate context and is never a stand-in for
    one account's portfolio.
    """

    record_ids = set(record.account_ids)
    context = set(context_ids)
    if record_ids:
        return bool(context) and record_ids <= context
    if record.account_bound:
        return not context
    return True


def partition_records(memory: TaskMemory, *, evaluation_time: datetime,
                      symbol: str | None = None, max_age_days: int | None = None,
                      historical_question: bool = False,
                      account_ids: tuple[str, ...] | None = None) -> MemoryPartition:
    """Stale or out-of-scope material must be revalidated or explicitly excluded."""

    now = utc(evaluation_time)
    scope = (symbol or memory.symbol or '').upper() or None
    accounts = (tuple(sorted(account_ids)) if account_ids is not None
                else tuple(sorted(memory.account_ids)))
    fresh, historical, revalidate, excluded = [], [], [], []
    for record in memory.records:
        if scope and record.symbol and record.symbol != scope:
            excluded.append((record.record_id, 'SCOPE_MISMATCH'))
            continue
        if not _accounts_compatible(record, accounts):
            excluded.append((record.record_id, 'SCOPE_MISMATCH'))
            continue
        if record.superseded_by:
            excluded.append((record.record_id, 'SUPERSEDED'))
            continue
        if record.as_of > now or _available_at(record) > now:
            excluded.append((record.record_id, 'NOT_YET_AVAILABLE'))
            continue
        if not record.pit_ok:
            excluded.append((record.record_id, 'NOT_YET_AVAILABLE'))
            continue
        aged = (max_age_days is not None
                and now - record.as_of > timedelta(days=max_age_days))
        if record.stale or aged:
            if historical_question and record.pit_ok and _available_at(record) <= now:
                historical.append(record.record_id)
            else:
                revalidate.append(record.record_id)
                excluded.append((record.record_id, 'STALE'))
            continue
        fresh.append(record.record_id)
    notes = []
    if revalidate:
        notes.append('STALE_REQUIRES_REVALIDATION')
    if any(reason == 'SCOPE_MISMATCH' for _, reason in excluded):
        notes.append('SCOPE_MISMATCH')
    return MemoryPartition(fresh=tuple(fresh), historical=tuple(historical),
                           stale_requiring_revalidation=tuple(revalidate),
                           excluded=tuple(excluded), notes=tuple(notes))


class FollowUpResolution(FrozenModel):
    is_follow_up: bool
    prior: PriorTaskSummary | None = None
    effective_symbol: str | None = None
    inherited_symbol: str | None = None
    inherited_goal: Goal = 'UNKNOWN'
    inherited_objective: Objective = 'UNSUPPORTED'
    account_ids: tuple[str, ...] = ()
    invalidated_fields: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    revalidate_record_ids: tuple[str, ...] = ()
    excluded_record_ids: tuple[str, ...] = ()
    thesis_record_id: str | None = None
    thesis_identity: MemoryIdentity | None = None
    price_assertion_record_id: str | None = None


def resolve_follow_up(memory: TaskMemory, question: str, *, explicit_symbols: tuple[str, ...] = (),
                      account_ids: tuple[str, ...] | None = None,
                      evaluation_time: datetime | None = None,
                      max_age_days: int | None = None,
                      historical_question: bool = False) -> FollowUpResolution:
    """Resolve inheritance and invalidation before the intent parser runs."""

    now = utc(evaluation_time) if evaluation_time is not None else memory.cutoff
    explicit = [s.removeprefix('$').upper() for s in explicit_symbols]
    invalidated: list[str] = []
    notes: list[str] = []

    effective_symbol = memory.symbol
    prior_symbol: str | None = memory.symbol
    prior_goal: Goal = memory.goal
    prior_objective: Objective = memory.objective
    if len(explicit) == 1 and explicit[0] != memory.symbol:
        effective_symbol = explicit[0]
        prior_symbol = None
        prior_goal = 'INVESTIGATE'
        prior_objective = 'EXPLAIN_CHANGE'
        invalidated += ['thesis', 'price_assertion', 'premises']
        notes.append('SYMBOL_SWITCHED')
    elif len(explicit) > 1:
        effective_symbol = None
        prior_symbol = None
        invalidated += ['symbol', 'thesis', 'price_assertion']
        notes.append('MULTIPLE_EXPLICIT_SYMBOLS')

    scope = tuple(sorted({s.strip() for s in account_ids})) if account_ids is not None else memory.account_ids
    scope_changed = account_ids is not None and tuple(sorted(memory.account_ids)) != scope
    if scope_changed:
        invalidated += ['portfolio_context', 'valuation', 'thesis']
        notes.append('ACCOUNT_SCOPE_CHANGED')
        prior_accounts: tuple[str, ...] = ()
    else:
        prior_accounts = memory.account_ids

    partition = partition_records(memory, evaluation_time=now, symbol=effective_symbol,
                                  max_age_days=max_age_days,
                                  historical_question=historical_question,
                                  account_ids=scope)
    if 'SCOPE_MISMATCH' in partition.notes:
        notes.append('SCOPE_MISMATCH')
    thesis = select_thesis(memory, symbol=effective_symbol, cutoff=now)
    if thesis is not None and thesis.record_id in partition.excluded_ids():
        thesis = None
    if scope_changed:
        thesis = None
    if thesis is not None and thesis.identity == 'AI_DRAFT':
        notes.append('AI_DRAFT_BASELINE_ONLY')
    price = select_user_price(memory, symbol=effective_symbol)
    if price is not None and price.record_id in partition.excluded_ids():
        price = None
    if price is not None and not assertion_literal_is_unchanged(price):
        price = None
        notes.append('PRICE_ASSERTION_REJECTED')
    if price is not None:
        notes.append('USER_PRICE_IS_NOT_A_MARKET_FACT')

    is_follow_up = not explicit and memory.goal != 'UNKNOWN'
    if is_follow_up:
        notes.append('FOLLOW_UP_INHERITS_SYMBOL_AND_SCOPE')
    prior = None
    if is_follow_up or (len(explicit) == 1 and scope == memory.account_ids):
        prior = PriorTaskSummary(task_id=memory.current_task_id,
                                 thread_root_id=memory.thread_root_id,
                                 goal=prior_goal, objective=prior_objective,
                                 symbol=prior_symbol, account_ids=prior_accounts,
                                 cutoff=memory.cutoff)
    return FollowUpResolution(
        is_follow_up=is_follow_up, prior=prior, effective_symbol=effective_symbol,
        inherited_symbol=memory.symbol if prior_symbol else None,
        inherited_goal=prior_goal, inherited_objective=prior_objective,
        account_ids=scope, invalidated_fields=tuple(dict.fromkeys(invalidated)),
        notes=tuple(notes),
        revalidate_record_ids=partition.stale_requiring_revalidation,
        excluded_record_ids=tuple(record_id for record_id, _ in partition.excluded),
        thesis_record_id=thesis.record_id if thesis else None,
        thesis_identity=thesis.identity if thesis else None,
        price_assertion_record_id=price.record_id if price else None)
