"""Deterministic, replay-ready foundation for an existing-holding decision."""

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Literal, NoReturn
from uuid import uuid4

from pydantic import Field, field_validator, model_validator, model_serializer

from .agent import MAX_EOD_AGE, snapshot_evidence
from .analytics import fresh_observations
from .clock import Clock, system_utc_now
from .domain import (EvidenceItem, FrozenModel, MarketToolArgs, PortfolioSnapshot,
                     PreviousDecisionToolArgs, PriceObservation, RouteIntent, RoutePlan,
                     RunTrace, SnapshotToolArgs, ThesisToolArgs, ToolCall, utc)
from .persistence import Store
from .provider import EODProvider, ProviderError, MarketDataResult
from .thesis import ThesisProfile
from .tdb_contract import SemanticArtifact
from .investment_context import (Intent, Cause, ScenarioAssumption, OwnerPolicy,
    InvestmentContextPacket, ConditionalAssessment, validate_conditional)


def content_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def price_difference_percentage(price: Decimal, close: Decimal | None) -> Decimal | None:
    if close is None:
        return None
    return ((price - close) * Decimal("100") / close).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


class TDBError(ValueError):
    def __init__(self, code: str, status_code: int = 422, run_id: str | None = None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.run_id = run_id


class TDBRequest(FrozenModel):
    symbol: str
    decision_price: Decimal
    currency: Literal["USD"] = "USD"
    source: Literal["USER_INPUT"] = "USER_INPUT"
    as_of: datetime | None = None

    _utc_as_of = field_validator("as_of")(lambda value: utc(value) if value else None)

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.^/\-]{0,14}", normalized):
            raise ValueError("invalid US equity symbol")
        return normalized

    @field_validator("decision_price")
    @classmethod
    def valid_price(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("Decision Price must be positive and finite")
        return value


class InvestmentContextRequest(TDBRequest):
    intent: Intent
    scenario_cause: Cause | None = None
    assumptions: tuple[ScenarioAssumption, ...] = Field(default=(), max_length=5)
    owner_policy: OwnerPolicy = Field(default_factory=OwnerPolicy)

    @model_validator(mode="after")
    def intent_is_explicit(self):
        if self.intent == "CURRENT":
            if self.scenario_cause is not None or self.assumptions:
                raise ValueError("current intent cannot assert hypothetical facts")
        elif not self.scenario_cause or not self.assumptions:
            raise ValueError("hypothetical intent requires cause and typed assumptions")
        ids = [a.assumption_id for a in self.assumptions]
        # Legacy conditions have no polarity contract. Never guess from their
        # prose or relabel them as adverse. Retain their old shape for readback.
        if any(a.kind == "THESIS_INVALIDATION" for a in self.assumptions):
            raise ValueError("legacy condition polarity is UNSPECIFIED; provide an independent adverse premise")
        if len(set(ids)) != len(ids) or any(a.kind == "ADVERSE_THESIS_PREMISE" for a in self.assumptions) != (self.scenario_cause == "THESIS_BREAK"):
            raise ValueError("assumption namespace or thesis cause mismatch")
        return self


class EvidenceReference(FrozenModel):
    evidence_id: str
    evidence_type: str
    provider: str
    source_record_id: str | None
    source_uri: str | None
    content_hash: str
    as_of: datetime | None
    published_at: datetime | None
    retrieved_at: datetime

    _utc_as_of = field_validator("as_of")(lambda value: utc(value) if value else None)
    _utc_published = field_validator("published_at")(lambda value: utc(value) if value else None)
    _utc_retrieved = field_validator("retrieved_at")(utc)

    @classmethod
    def from_evidence(cls, item: EvidenceItem) -> "EvidenceReference":
        return cls.model_validate(item.model_dump(exclude={"title", "symbol", "excerpt", "structured_data", "raw_ref"}))


class EvidenceManifest(FrozenModel):
    version: Literal[1, 2] = 1
    evidence_cutoff: datetime
    snapshot_id: str
    snapshot_hash: str
    market_observation: PriceObservation | None
    thesis: ThesisProfile | None
    previous_qualified_record_id: str | None
    evidence: tuple[EvidenceReference, ...]
    normalized_input_hash: str
    run_id: str
    tool_names: tuple[str, ...]
    provider_name: str
    model_name: str | None = None
    prompt_contract: str | None = None
    claim_links: dict[str, list[str]] = Field(default_factory=dict)
    sec_accessions: dict[str, str] = Field(default_factory=dict)
    manifest_hash: str

    _utc_cutoff = field_validator("evidence_cutoff")(utc)

    @model_validator(mode="after")
    def verify_hash_and_references(self):
        payload = self.model_dump(mode="json", exclude={"manifest_hash"})
        valid_hashes = {content_hash(payload)}
        if self.version == 1 and not self.sec_accessions:
            valid_hashes.add(content_hash({key: value for key, value in payload.items()
                                           if key != "sec_accessions"}))
        if self.manifest_hash not in valid_hashes:
            raise ValueError("evidence manifest hash mismatch")
        ids = [item.evidence_id for item in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate evidence ID in manifest")
        if any(set(linked) - set(ids) for linked in self.claim_links.values()):
            raise ValueError("claim cites evidence outside manifest")
        sec_ids = {item.evidence_id for item in self.evidence if item.evidence_type == "sec_filing"}
        if set(self.sec_accessions) - sec_ids:
            raise ValueError("SEC accession cites evidence outside manifest")
        return self


class DecisionRecord(FrozenModel):
    version: Literal[1, 2, 3] = 1
    decision_id: str
    symbol: str
    decision_price: Decimal
    decision_price_source: Literal["USER_INPUT", "COMPLETED_CLOSE"] = "USER_INPUT"
    currency: Literal["USD"] = "USD"
    analysis_cutoff: datetime
    recorded_at: datetime
    portfolio_snapshot_id: str
    portfolio_snapshot_hash: str
    holding_position_ids: tuple[str, ...]
    previous_close: PriceObservation | None
    price_difference: Decimal | None
    thesis_version_id: str | None
    previous_qualified_record_id: str | None
    review_state: Literal["FIRST_REVIEW", "PRIOR_QUALIFIED_REVIEW"]
    # This slice creates only FOUNDATION_ONLY. Later semantic TDB can create QUALIFIED_REVIEW.
    record_kind: Literal["FOUNDATION_ONLY", "QUALIFIED_REVIEW", "SEMANTIC_ABSTENTION",
                         "SCENARIO_REVIEW", "SCENARIO_ABSTENTION"] = "FOUNDATION_ONLY"
    manifest: EvidenceManifest
    run_id: str
    semantic: SemanticArtifact | None = None
    user_decision: None = None
    outcome_status: Literal["PENDING"] = "PENDING"
    record_hash: str
    price_difference_pct: Decimal | None = None
    investment_context: InvestmentContextPacket | None = None
    conditional_assessment: ConditionalAssessment | None = None

    @model_serializer(mode="wrap")
    def preserve_pre_idc_shape(self, handler):
        payload = handler(self)
        if self.version < 3:
            for key in ("investment_context", "conditional_assessment"):
                if key not in self.model_fields_set:
                    payload.pop(key, None)
        return payload

    _utc_cutoff = field_validator("analysis_cutoff")(utc)
    _utc_recorded = field_validator("recorded_at")(utc)

    @model_validator(mode="after")
    def verify_record(self):
        if self.recorded_at < self.analysis_cutoff:
            raise ValueError("decision cannot be recorded before its evidence cutoff")
        payload = self.model_dump(mode="json", exclude={"record_hash"})
        original_payload_hash = content_hash(payload)
        # Additive v5 semantic fields are absent from old immutable payloads. Validate
        # their original hash without rewriting the stored record or accepting edits.
        if (self.semantic is not None and self.semantic.version == 1 and
                self.semantic.depth is None and self.semantic.context_evidence_id is None and self.semantic.research is None):
            payload["semantic"] = {key: value for key, value in payload["semantic"].items()
                                   if key not in {"depth", "context_evidence_id", "research"}}
        valid_hashes = {content_hash(payload), original_payload_hash}
        if self.price_difference_pct is None:
            valid_hashes.add(content_hash({key: value for key, value in payload.items()
                                           if key != "price_difference_pct"}))
        if self.version == 1 and self.semantic is None:
            legacy = {key: value for key, value in payload.items() if key != "semantic"}
            valid_hashes.add(content_hash(legacy))
            if self.price_difference_pct is None:
                valid_hashes.add(content_hash({key: value for key, value in legacy.items()
                                               if key != "price_difference_pct"}))
            if self.manifest.version == 1 and not self.manifest.sec_accessions:
                legacy["manifest"] = {key: value for key, value in legacy["manifest"].items()
                                      if key != "sec_accessions"}
                valid_hashes.add(content_hash(legacy))
                if self.price_difference_pct is None:
                    valid_hashes.add(content_hash({key: value for key, value in legacy.items()
                                                   if key != "price_difference_pct"}))
        if self.record_hash not in valid_hashes:
            raise ValueError("decision record hash mismatch")
        if (self.manifest.evidence_cutoff != self.analysis_cutoff
                or self.manifest.snapshot_id != self.portfolio_snapshot_id
                or self.manifest.snapshot_hash != self.portfolio_snapshot_hash
                or self.manifest.previous_qualified_record_id != self.previous_qualified_record_id
                or self.manifest.run_id != self.run_id
                or self.manifest.market_observation != self.previous_close):
            raise ValueError("decision record and manifest disagree")
        if self.price_difference is not None and self.previous_close is None:
            raise ValueError("price difference requires an eligible close")
        if self.decision_price_source == "COMPLETED_CLOSE" and (
            self.previous_close is None or self.decision_price != self.previous_close.price
        ):
            raise ValueError("system decision price must equal the frozen eligible close")
        if self.price_difference is not None and self.price_difference != self.decision_price - self.previous_close.price:
            raise ValueError("price difference mismatch")
        if self.previous_close is not None and self.price_difference is None:
            raise ValueError("eligible close requires a price difference")
        if self.price_difference_pct is not None and self.price_difference_pct != (
            price_difference_percentage(self.decision_price,
                self.previous_close.price if self.previous_close else None)
        ):
            raise ValueError("percentage price difference mismatch")
        normalized = {
            "symbol": self.symbol, "decision_price": str(self.decision_price),
            "source": self.decision_price_source, "currency": self.currency,
            "cutoff": self.analysis_cutoff.isoformat(),
            "snapshot_id": self.portfolio_snapshot_id,
            "snapshot_hash": self.portfolio_snapshot_hash,
            "holding_position_ids": self.holding_position_ids,
            "market_observation": self.previous_close.model_dump(mode="json") if self.previous_close else None,
            "thesis": self.manifest.thesis.model_dump(mode="json") if self.manifest.thesis else None,
            "previous_qualified_record_id": self.previous_qualified_record_id,
        }
        if self.version == 3:
            if self.investment_context is None:
                raise ValueError("IDC record needs context packet")
            packet = self.investment_context
            if (packet.cutoff != self.analysis_cutoff or packet.symbol != self.symbol
                or packet.decision_price_usd != self.decision_price
                or packet.decision_price_source != self.decision_price_source
                or set(packet.evidence_ids)-{r.evidence_id for r in self.manifest.evidence}):
                raise ValueError("IDC packet and record mismatch")
            if any(c.snapshot_id != self.portfolio_snapshot_id or c.snapshot_hash != self.portfolio_snapshot_hash
                   for c in packet.portfolio_constraints):
                raise ValueError("IDC portfolio snapshot mismatch")
            actual_company = {r.evidence_id for r in self.manifest.evidence
                if r.evidence_type in {"news","sec_filing"} and r.published_at is not None
                and r.published_at<=self.analysis_cutoff and r.retrieved_at<=self.analysis_cutoff}
            if set(packet.actual_company_evidence_ids)-actual_company:
                raise ValueError("IDC company context must use eligible actual company evidence")
            validate_conditional(self.conditional_assessment, packet, self.semantic.thesis_delta if self.semantic else None)
            normalized["investment_context_hash"] = packet.packet_hash
            if (self.record_kind in {"SCENARIO_REVIEW", "SCENARIO_ABSTENTION"}) != (packet.intent == "HYPOTHETICAL"):
                raise ValueError("scenario qualification cannot enter current history")
        elif self.investment_context is not None or self.conditional_assessment is not None:
            raise ValueError("legacy record cannot contain IDC context")
        if self.manifest.normalized_input_hash != content_hash(normalized):
            raise ValueError("normalized input hash mismatch")
        if self.thesis_version_id != (
            self.manifest.thesis.thesis_version_id if self.manifest.thesis else None
        ):
            raise ValueError("thesis version mismatch")
        if self.review_state != ("FIRST_REVIEW" if self.previous_qualified_record_id is None else "PRIOR_QUALIFIED_REVIEW"):
            raise ValueError("previous review state mismatch")
        if self.record_kind == "QUALIFIED_REVIEW" and not self.manifest.claim_links:
            raise ValueError("qualified review requires grounded claims")
        if self.version >= 2:
            if self.semantic is None or self.record_kind == "FOUNDATION_ONLY" or self.manifest.version != 2:
                raise ValueError("semantic record requires a semantic artifact")
            if (self.record_kind in {"QUALIFIED_REVIEW", "SCENARIO_REVIEW"}) != (
                self.semantic.assessment.assessment_status == "ASSESSED"
            ):
                raise ValueError("semantic record qualification mismatch")
            ids = {item.evidence_id for item in self.manifest.evidence}
            cited = set(self.semantic.current_evidence_ids)
            cited.update(self.semantic.new_evidence_ids)
            cited.update(self.semantic.retained_counter_evidence_ids)
            cited.update(self.semantic.thesis_delta.evidence_ids)
            cited.update(self.semantic.assessment.supporting_evidence_ids)
            cited.update(self.semantic.assessment.counter_evidence_ids)
            for rationale in self.semantic.assessment.rationales:
                cited.update(rationale.evidence_ids)
            if self.semantic.depth:
                from .tdb_semantic import depth_statements
                for statement in depth_statements(self.semantic.depth):
                    cited.update(statement.evidence_ids)
            if self.semantic.context_evidence_id:
                cited.add(self.semantic.context_evidence_id)
            if self.semantic.research:
                cited.update(self.semantic.research.supplemental_evidence_ids)
            if cited - ids or self.manifest.model_name != self.semantic.model_name:
                raise ValueError("semantic references or model mismatch")
        elif self.semantic is not None or self.record_kind == "SEMANTIC_ABSTENTION":
            raise ValueError("version one cannot contain semantic assessment")
        return self


def _pit_eligible(item: EvidenceItem, cutoff: datetime, *, historical: bool) -> bool:
    if item.as_of is not None and item.as_of > cutoff:
        return False
    if item.published_at is not None and item.published_at > cutoff:
        return False
    # Historical provider content without a publication/vintage date cannot be backdated.
    if historical and item.retrieved_at > cutoff and item.published_at is None:
        return False
    return True


def _observation_matches_evidence(observation: PriceObservation, item: EvidenceItem) -> bool:
    data = item.structured_data
    if not isinstance(data, dict):
        return False
    try:
        data_time = utc(datetime.fromisoformat(str(data["as_of"])))
        data_price = Decimal(str(data["price"]))
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return False
    return (item.evidence_type == "market_data"
            and item.provider == observation.provider
            and item.symbol == observation.symbol
            and item.as_of == observation.as_of
            and data.get("symbol") == observation.symbol
            and data.get("provider") == observation.provider
            and data.get("currency") == observation.currency
            and data_time == observation.as_of
            and data_price == observation.price)


def build_foundation(request: TDBRequest, store: Store, market: EODProvider,
                     clock: Clock = system_utc_now) -> DecisionRecord:
    started_at = utc(clock())
    cutoff = request.as_of or started_at
    input_cutoff = cutoff
    if cutoff > started_at:
        raise TDBError("FUTURE_CUTOFF")
    snapshot_started = utc(clock())
    snapshot: PortfolioSnapshot | None = store.get_snapshot_known_at(cutoff)
    snapshot_ended = utc(clock())
    if snapshot is None:
        raise TDBError("INSUFFICIENT_HISTORY", 409)
    held = tuple(position.position_id for position in snapshot.positions
                 if position.symbol == request.symbol)
    if not held:
        raise TDBError("UNHELD_SYMBOL")

    run_id = str(uuid4())
    tool_names = ("portfolio.get_snapshot", "market.get_eod", "thesis.get",
                  "decision.get_previous_review")
    selected: list[EvidenceItem] = [snapshot_evidence(snapshot, snapshot_ended)]
    market_retry_failures = []

    def append_market_attempts(calls):
        for began, ended, code in market_retry_failures:
            calls.append(ToolCall(sequence=len(calls)+1, name="market.get_eod",
                arguments=MarketToolArgs(symbols=[request.symbol], as_of=input_cutoff),
                status="error", started_at=began, ended_at=ended, error=code))

    def persist_failure(code: str, status_code: int, calls: list[ToolCall]) -> NoReturn:
        attempted_evidence = list({item.evidence_id: item for item in selected}.values())
        trace = RunTrace(
            run_id=run_id, query=f"TDB-1 foundation: {request.symbol}",
            requested_as_of=request.as_of,
            route_plan=RoutePlan(intent=RouteIntent.TRIGGERED_DECISION_BRIEF,
                                 symbols=[request.symbol], requested_as_of=request.as_of,
                                 required_tools=list(tool_names), needs_semantic_judgment=False),
            tool_calls=calls, synthesis=None,
            evidence_ids=[item.evidence_id for item in attempted_evidence],
            final_claim_links={},
        )
        store.record_failed_decision_run(trace, attempted_evidence)
        raise TDBError(code, status_code, run_id)

    def fail_market(code: str, provider_code: str, ended_at: datetime) -> NoReturn:
        calls = [
            ToolCall(sequence=1, name=tool_names[0],
                     arguments=SnapshotToolArgs(as_of=input_cutoff), status="success",
                     started_at=snapshot_started, ended_at=snapshot_ended,
                     output_refs=[snapshot.snapshot_id, selected[0].evidence_id]),
        ]
        append_market_attempts(calls)
        calls.append(ToolCall(sequence=len(calls)+1, name=tool_names[1],
            arguments=MarketToolArgs(symbols=[request.symbol], as_of=input_cutoff),
            status="error", started_at=attempt_started, ended_at=ended_at,
            output_refs=[], error=provider_code))
        persist_failure(code, 503, calls)

    market_started = utc(clock())
    for attempt in range(2):
        attempt_started = utc(clock())
        try:
            market_result = market.retrieve_eod([request.symbol], cutoff)
        except ProviderError as exc:
            if attempt == 0 and exc.code in {"transport", "timeout", "retryable"}:
                market_retry_failures.append((attempt_started, utc(clock()), exc.code))
                continue
            if isinstance(request, InvestmentContextRequest):
                # IDC can compare a USER_INPUT price without a market reference.
                # Retain failed attempts in trace; never manufacture a close.
                market_retry_failures.append((attempt_started, utc(clock()), exc.code))
                market_result = MarketDataResult(observations=[], evidence=[])
                break
            fail_market("MARKET_DATA_UNAVAILABLE", exc.code, utc(clock()))
        else:
            break
    market_ended = utc(clock())
    if any(observation.symbol != request.symbol or observation.as_of > cutoff
           for observation in market_result.observations):
        fail_market("MARKET_DATA_INVALID", "invalid_data", market_ended)
    market_items = {item.evidence_id: item for item in market_result.evidence}
    if any(observation.evidence_id not in market_items
           or not _observation_matches_evidence(observation,
                                                market_items[observation.evidence_id])
           for observation in market_result.observations):
        fail_market("MARKET_DATA_INVALID", "invalid_data", market_ended)
    eligible, _ = fresh_observations(market_result.observations, cutoff, MAX_EOD_AGE)
    eligible = [item for item in eligible
                if item.evidence_id in market_items
                and market_items[item.evidence_id].as_of == item.as_of
                and _pit_eligible(market_items[item.evidence_id], cutoff,
                                  historical=request.as_of is not None)]
    close = max(eligible, key=lambda item: (item.as_of, item.evidence_id), default=None)
    if close is not None:
        selected.append(market_items[close.evidence_id])
    if request.source == "COMPLETED_CLOSE" and (
        close is None or close.price != request.decision_price or
        close.evidence_id != request.decision_framing.price_evidence_id
    ):
        fail_market("MARKET_DATA_INVALID", "invalid_data", market_ended)

    thesis_started = utc(clock())

    def fail_thesis() -> NoReturn:
        calls = [
            ToolCall(sequence=1, name=tool_names[0],
                     arguments=SnapshotToolArgs(as_of=input_cutoff), status="success",
                     started_at=snapshot_started, ended_at=snapshot_ended,
                     output_refs=[snapshot.snapshot_id, selected[0].evidence_id]),
            ToolCall(sequence=2, name=tool_names[1],
                     arguments=MarketToolArgs(symbols=[request.symbol], as_of=input_cutoff),
                     status="success", started_at=market_started, ended_at=market_ended,
                     output_refs=[close.evidence_id] if close else []),
            ToolCall(sequence=3, name=tool_names[2],
                     arguments=ThesisToolArgs(symbol=request.symbol, as_of=input_cutoff),
                     status="error", started_at=thesis_started, ended_at=utc(clock()),
                     output_refs=[], error="THESIS_EVIDENCE_UNAVAILABLE"),
        ]
        append_market_attempts(calls)
        persist_failure("THESIS_EVIDENCE_UNAVAILABLE", 409, calls)

    thesis = store.get_thesis(request.symbol, cutoff)
    if thesis is not None:
        thesis_item = store.get_thesis_evidence(thesis.thesis_version_id)
        if thesis_item is None or not _pit_eligible(thesis_item, cutoff,
                                                    historical=request.as_of is not None):
            fail_thesis()
        selected.append(thesis_item)
        for evidence_id in thesis.evidence_ids:
            item = store.get_thesis_source_evidence(
                evidence_id, min(cutoff, thesis_item.retrieved_at))
            if item is None or not _pit_eligible(item, cutoff,
                                                 historical=request.as_of is not None):
                fail_thesis()
            selected.append(item)
    thesis_ended = utc(clock())

    # For a current run, the frozen evidence cutoff follows retrieval. Historical runs
    # retain the caller's exact cutoff and require a provable earlier input vintage.
    if request.as_of is None:
        cutoff = utc(clock())
        if any(item.retrieved_at > cutoff for item in selected):
            raise TDBError("EVIDENCE_AFTER_CUTOFF", 409)

    previous_started = utc(clock())
    previous = store.get_previous_qualified_decision(request.symbol, cutoff)
    previous_ended = utc(clock())
    previous_id = previous.decision_id if previous else None
    selected = list({item.evidence_id: item for item in selected}.values())
    if market_retry_failures:
        tool_names = (*tool_names, *("market.get_eod" for _ in market_retry_failures))
    refs = tuple(EvidenceReference.from_evidence(item) for item in selected)
    normalized = {
        "symbol": request.symbol, "decision_price": str(request.decision_price),
        "source": request.source, "currency": request.currency,
        "cutoff": cutoff.isoformat(), "snapshot_id": snapshot.snapshot_id,
        "snapshot_hash": snapshot.content_hash, "holding_position_ids": held,
        "market_observation": close.model_dump(mode="json") if close else None,
        "thesis": thesis.model_dump(mode="json") if thesis else None,
        "previous_qualified_record_id": previous_id,
    }
    manifest_fields = dict(
        version=1, evidence_cutoff=cutoff, snapshot_id=snapshot.snapshot_id,
        snapshot_hash=snapshot.content_hash, market_observation=close, thesis=thesis,
        previous_qualified_record_id=previous_id, evidence=refs,
        normalized_input_hash=content_hash(normalized), run_id=run_id,
        tool_names=tool_names, provider_name=market.name,
        model_name=None, prompt_contract=None, claim_links={},
    )
    manifest_payload = EvidenceManifest.model_construct(**manifest_fields).model_dump(mode="json")
    manifest = EvidenceManifest(**manifest_fields, manifest_hash=content_hash(manifest_payload))
    recorded_at = utc(clock())
    record_fields = dict(
        version=1, decision_id=str(uuid4()), symbol=request.symbol,
        decision_price=request.decision_price, decision_price_source=request.source,
        currency=request.currency, analysis_cutoff=cutoff, recorded_at=recorded_at,
        portfolio_snapshot_id=snapshot.snapshot_id, portfolio_snapshot_hash=snapshot.content_hash,
        holding_position_ids=held, previous_close=close,
        price_difference=request.decision_price - close.price if close else None,
        thesis_version_id=thesis.thesis_version_id if thesis else None,
        previous_qualified_record_id=previous_id,
        review_state="FIRST_REVIEW" if previous is None else "PRIOR_QUALIFIED_REVIEW",
        record_kind="FOUNDATION_ONLY", manifest=manifest, run_id=run_id,
        user_decision=None, outcome_status="PENDING",
    )
    record_payload = DecisionRecord.model_construct(**record_fields).model_dump(mode="json")
    record = DecisionRecord(**record_fields, record_hash=content_hash(record_payload))
    calls = [
        ToolCall(sequence=1, name=tool_names[0], arguments=SnapshotToolArgs(as_of=input_cutoff),
                 status="success", started_at=snapshot_started, ended_at=snapshot_ended,
                 output_refs=[snapshot.snapshot_id, selected[0].evidence_id]),
        ToolCall(sequence=2, name=tool_names[1],
                 arguments=MarketToolArgs(symbols=[request.symbol], as_of=input_cutoff),
                 status="success", started_at=market_started, ended_at=market_ended,
                 output_refs=[close.evidence_id] if close else []),
        ToolCall(sequence=3, name=tool_names[2],
                 arguments=ThesisToolArgs(symbol=request.symbol, as_of=input_cutoff),
                 status="success", started_at=thesis_started, ended_at=thesis_ended,
                 output_refs=[thesis.thesis_version_id] if thesis else []),
        ToolCall(sequence=4, name=tool_names[3],
                 arguments=PreviousDecisionToolArgs(symbol=request.symbol, before=cutoff),
                 status="success", started_at=previous_started, ended_at=previous_ended,
                 output_refs=[previous_id] if previous_id else []),
    ]
    append_market_attempts(calls)
    trace = RunTrace(
        run_id=run_id, query=f"TDB-1 foundation: {request.symbol}",
        requested_as_of=request.as_of,
        route_plan=RoutePlan(intent=RouteIntent.TRIGGERED_DECISION_BRIEF,
                             symbols=[request.symbol], requested_as_of=request.as_of,
                             required_tools=list(tool_names), needs_semantic_judgment=False),
        tool_calls=calls, synthesis=None,
        evidence_ids=[item.evidence_id for item in selected], final_claim_links={},
    )
    store.add_decision(record, trace, selected)
    return record
