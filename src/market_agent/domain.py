"""Typed contracts shared by the first vertical slice."""

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _deep_freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value):
    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value


class Account(FrozenModel):
    account_id: str
    name: str
    cash: Decimal

    @field_validator("cash")
    @classmethod
    def valid_cash(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("cash must be finite and nonnegative")
        return value


class Position(FrozenModel):
    position_id: str
    account_id: str
    symbol: str
    quantity: Decimal
    avg_cost: Decimal
    user_stage: str = "hold"
    note: str = ""

    @field_validator("quantity")
    @classmethod
    def valid_quantity(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("quantity must be finite and positive")
        return value

    @field_validator("avg_cost")
    @classmethod
    def valid_cost(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("avg_cost must be finite and nonnegative")
        return value


class InstrumentOverride(FrozenModel):
    name: str | None = None
    sector: str | None = None
    theme: str | None = None


class SourceMetadata(FrozenModel):
    kind: Literal["capital_desk_v1_import", "manual_entry", "csv_import"]
    imported_at: datetime
    source_hash: str
    legacy_manual_prices: Mapping[str, Mapping[str, object]] = Field(default_factory=dict)

    _normalize_imported_at = field_validator("imported_at")(utc)

    @model_validator(mode="after")
    def freeze_provenance(self):
        object.__setattr__(self, "legacy_manual_prices", _deep_freeze(self.legacy_manual_prices))
        return self

    @field_serializer("legacy_manual_prices")
    def serialize_legacy_manual_prices(self, value):
        return _deep_thaw(value)


class PortfolioContent(FrozenModel):
    schema_version: Literal[1] = 1
    portfolio_id: str = "default"
    base_currency: Literal["USD"] = "USD"
    as_of: datetime
    accounts: tuple[Account, ...]
    positions: tuple[Position, ...]
    watchlist: tuple[str, ...] = ()
    instrument_overrides: Mapping[str, InstrumentOverride] = Field(default_factory=dict)
    source: SourceMetadata

    _normalize_as_of = field_validator("as_of")(utc)

    @model_validator(mode="after")
    def check_references(self):
        ids = [account.account_id for account in self.accounts]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate account ID")
        keys = [(position.account_id, position.symbol) for position in self.positions]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate account/symbol position")
        if any(position.account_id not in ids for position in self.positions):
            raise ValueError("position refers to unknown account")
        object.__setattr__(self, "instrument_overrides", MappingProxyType(dict(self.instrument_overrides)))
        return self

    @field_serializer("instrument_overrides")
    def serialize_instrument_overrides(self, value):
        return dict(value)


class PortfolioSnapshot(PortfolioContent):
    snapshot_id: str
    content_hash: str

    @model_validator(mode="after")
    def hash_matches_content(self):
        if self.content_hash != portfolio_content_hash(self):
            raise ValueError("content_hash does not match canonical snapshot content")
        return self


def portfolio_content_hash(content: PortfolioContent) -> str:
    payload = content.model_dump(mode="json", exclude={"snapshot_id", "content_hash"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PriceObservation(FrozenModel):
    symbol: str
    price: Decimal
    as_of: datetime
    currency: Literal["USD"] = "USD"
    provider: str
    evidence_id: str

    _normalize_as_of = field_validator("as_of")(utc)

    @field_validator("price")
    @classmethod
    def valid_price(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("price must be finite and positive")
        return value


class RouteIntent(str, Enum):
    ATTENTION_SCAN = "attention_scan"
    DAILY_PORTFOLIO = "daily_portfolio"
    STOCK_EVENT_THESIS = "stock_event_thesis"
    TRIGGERED_DECISION_BRIEF = "triggered_decision_brief"
    PORTFOLIO_WHAT_IF = "portfolio_what_if"
    UNSUPPORTED = "unsupported"


class RoutePlan(FrozenModel):
    intent: RouteIntent
    symbols: list[str] = []
    requested_as_of: datetime | None = None
    required_tools: list[str] = []
    needs_semantic_judgment: bool = False
    confidence: Literal["high", "medium", "low"] = "high"

    _normalize_requested_as_of = field_validator("requested_as_of")(lambda v: utc(v) if v else None)


class EvidenceItem(FrozenModel):
    evidence_id: str
    evidence_type: Literal[
        "portfolio_snapshot",
        "market_data",
        "news",
        "sec_filing",
        "thesis",
        "calculation",
        "semantic_judgment",
    ]
    provider: str
    symbol: str | None = None
    title: str | None = None
    source_uri: str | None = None
    source_record_id: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime
    as_of: datetime | None = None
    content_hash: str
    excerpt: str | None = None
    structured_data: dict | None = None
    raw_ref: str | None = None

    _normalize_retrieved_at = field_validator("retrieved_at")(utc)
    _normalize_as_of = field_validator("as_of")(lambda v: utc(v) if v else None)
    _normalize_published_at = field_validator("published_at")(lambda v: utc(v) if v else None)


class GroundedClaim(FrozenModel):
    claim_id: str
    claim_type: Literal["fact", "calculation", "judgment", "recommendation"]
    text: str
    evidence_ids: list[str]

    @field_validator("evidence_ids")
    @classmethod
    def nonempty_evidence(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("a material claim requires evidence")
        return value


class AgentAnswer(FrozenModel):
    run_id: str
    answer: str
    claims: list[GroundedClaim]
    recommendation: None = None
    evidence: list[EvidenceItem]
    limitations: list[str] = []

    @model_validator(mode="after")
    def claims_are_grounded(self):
        ids = {item.evidence_id for item in self.evidence}
        if any(set(claim.evidence_ids) - ids for claim in self.claims):
            raise ValueError("claim cites evidence absent from answer")
        return self


class QueryRequest(FrozenModel):
    query: str
    as_of: datetime | None = None

    _normalize_as_of = field_validator("as_of")(lambda v: utc(v) if v else None)


class SnapshotToolArgs(FrozenModel):
    as_of: datetime | None = None

    _normalize_as_of = field_validator("as_of")(lambda v: utc(v) if v else None)


class MarketToolArgs(FrozenModel):
    symbols: list[str]
    as_of: datetime

    _normalize_as_of = field_validator("as_of")(utc)


class AnalyticsToolArgs(FrozenModel):
    snapshot_id: str
    price_evidence_ids: list[str]


class NewsSearchToolArgs(FrozenModel):
    symbols: list[str]
    since: datetime
    until: datetime
    limit: int

    _normalize_since = field_validator("since")(utc)
    _normalize_until = field_validator("until")(utc)


class SECSearchToolArgs(FrozenModel):
    symbol: str
    query: str
    forms: list[str]
    as_of: datetime
    limit: int

    _normalize_as_of = field_validator("as_of")(utc)


class ThesisToolArgs(FrozenModel):
    symbol: str
    as_of: datetime

    _normalize_as_of = field_validator("as_of")(utc)


class PreviousDecisionToolArgs(FrozenModel):
    symbol: str
    before: datetime

    _normalize_before = field_validator("before")(utc)


class SemanticToolArgs(FrozenModel):
    symbol: str
    thesis_version_id: str | None
    evidence_ids: list[str]


class ToolCall(FrozenModel):
    sequence: int
    name: str
    arguments: (
        SnapshotToolArgs
        | MarketToolArgs
        | AnalyticsToolArgs
        | NewsSearchToolArgs
        | SECSearchToolArgs
        | ThesisToolArgs
        | PreviousDecisionToolArgs
        | SemanticToolArgs
        | None
    )
    status: Literal["success", "error"]
    started_at: datetime
    ended_at: datetime
    output_refs: list[str] = []
    error: str | None = None

    _normalize_started_at = field_validator("started_at")(utc)
    _normalize_ended_at = field_validator("ended_at")(utc)


class SynthesisEvent(FrozenModel):
    model: str
    purpose: str
    status: Literal["success", "error"] = "success"
    started_at: datetime
    ended_at: datetime
    evidence_ids: list[str]
    claim_links: dict[str, list[str]]
    error: str | None = None
    validation_outcome: str | None = None
    provider: str | None = None
    prompt_contract: str | None = None
    attempt: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    usage: dict[str, int] = {}
    transport_attempts: list[dict] = []
    validation_attempts: list[dict] = []
    context_metrics: dict[str, int | str] = {}

    _normalize_started_at = field_validator("started_at")(utc)
    _normalize_ended_at = field_validator("ended_at")(utc)


class RunTrace(FrozenModel):
    run_id: str
    query: str
    requested_as_of: datetime | None
    route_plan: RoutePlan
    tool_calls: list[ToolCall]
    synthesis: SynthesisEvent | None
    model_events: list[SynthesisEvent] = []
    evidence_ids: list[str]
    final_claim_links: dict[str, list[str]]

    _normalize_requested_as_of = field_validator("requested_as_of")(lambda v: utc(v) if v else None)


class RunRecord(FrozenModel):
    status: Literal["running", "success", "error"]
    trace: RunTrace | None
    answer: AgentAnswer | None


class HealthResponse(FrozenModel):
    status: Literal["ok"] = "ok"
