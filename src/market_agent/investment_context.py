"""IDC v1: deterministic context and isolated assumptions, never provider truth."""
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .domain import FrozenModel, utc
from .provider import EASTERN

Finite = Annotated[Decimal, Field(allow_inf_nan=False)]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Intent = Literal["CURRENT", "HYPOTHETICAL"]
Cause = Literal["PRICE_ONLY_NO_NEW_DAMAGE", "MARKET_OR_SECTOR_RISK_OFF",
                "VALUATION_COMPRESSION", "COMPANY_SPECIFIC_DAMAGE", "THESIS_BREAK",
                "OTHER_EXPLICIT_ASSUMPTION"]


def digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


class ScenarioAssumption(FrozenModel):
    assumption_id: str = Field(pattern=r"^A:[a-zA-Z0-9_-]{1,64}$")
    kind: Literal["CAUSE", "BUSINESS_CONDITION", "THESIS_INVALIDATION", "ADVERSE_THESIS_PREMISE"]
    statement: str = Field(min_length=5, max_length=500)
    invalidation_condition_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def named_break(self):
        if not self.statement.strip():
            raise ValueError("empty assumption")
        if (self.kind == "THESIS_INVALIDATION") != (self.invalidation_condition_index is not None):
            raise ValueError("assumed thesis break must name an existing condition index")
        return self


class OwnerPolicy(FrozenModel):
    status: Literal["CONFIGURED", "NOT_CONFIGURED"] = "NOT_CONFIGURED"
    provenance: Literal["EXPLICIT_REQUEST", "UNKNOWN"] = "UNKNOWN"
    investment_horizon: Literal["WEEKS_MONTHS", "YEARS", "UNKNOWN"] = "UNKNOWN"
    single_name_nav_limit_pct: Positive | None = Field(default=None, le=100)
    average_down: Literal["ALLOW", "AVOID", "UNKNOWN"] = "UNKNOWN"
    goal: Literal["GROWTH", "PRESERVATION", "BALANCED", "UNKNOWN"] = "UNKNOWN"

    @model_validator(mode="after")
    def explicit_policy(self):
        known = self.single_name_nav_limit_pct is not None or any(value != "UNKNOWN"
            for value in (self.investment_horizon, self.average_down, self.goal))
        if self.status == "NOT_CONFIGURED" and (known or self.provenance != "UNKNOWN"):
            raise ValueError("missing policy cannot contain preferences")
        if self.status == "CONFIGURED" and (not known or self.provenance != "EXPLICIT_REQUEST"):
            raise ValueError("configured policy needs explicit preferences")
        return self


class PortfolioConstraint(FrozenModel):
    metric: Literal["POSITION_NAV_WEIGHT_PCT", "POSITION_COST_EXPOSURE_PCT"]
    value: Finite | None
    numerator_usd: Finite | None
    denominator_usd: Finite | None
    numerator_semantics: str
    denominator_semantics: str
    basis: Literal["USER_PRICE_TARGET_PLUS_ELIGIBLE_EOD_OTHERS", "COMPLETED_CLOSE_TARGET_PLUS_ELIGIBLE_EOD_OTHERS", "PORTFOLIO_COST"]
    status: Literal["KNOWN", "UNKNOWN"]
    missing_symbols: tuple[str, ...] = ()
    as_of: datetime
    snapshot_id: str
    snapshot_hash: str
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    binding_to_explicit_policy: bool | None = None
    _utc = field_validator("as_of")(utc)

    @model_validator(mode="after")
    def arithmetic_and_scope(self):
        if self.status == "UNKNOWN":
            if self.value is not None or self.denominator_usd is not None or self.binding_to_explicit_policy is not None:
                raise ValueError("unknown valuation cannot imply a weight or binding limit")
        elif self.missing_symbols or self.numerator_usd is None or not self.denominator_usd or self.value != self.numerator_usd*100/self.denominator_usd:
            raise ValueError("constraint value/basis mismatch")
        return self


def completed_session_end(day: date):
    return datetime.combine(day+timedelta(days=1), datetime.min.time(), EASTERN).astimezone(timezone.utc)-timedelta(microseconds=1)


class CompletedClose(FrozenModel):
    session_date: date
    available_at: datetime
    close: Positive
    evidence_id: str = Field(min_length=1)
    _utc = field_validator("available_at")(utc)


class CloseHistory(FrozenModel):
    """Normalized trusted-source input, never accepted from the decision request."""
    symbol: str
    price_basis: Literal["UNADJUSTED_NO_SPLITS", "SPLIT_ADJUSTED", "UNVERIFIED"]
    corporate_actions_verified: bool = False
    completeness: Literal["PROVIDED_SESSIONS", "UNVERIFIED"] = "UNVERIFIED"
    source: str
    bars: tuple[CompletedClose, ...]
    source_evidence_ids: tuple[str, ...] = ()


class MarketWindow(FrozenModel):
    sessions: Literal[20, 60, 252]
    status: Literal["AVAILABLE", "UNKNOWN"]
    reason: str | None = None
    start: date | None = None
    end: date | None = None
    close_low: Positive | None = None
    close_high: Positive | None = None
    decision_price_range_position_pct: Finite | None = None
    decision_price_drawdown_from_close_high_pct: Finite | None = None
    window_close_change_pct: Finite | None = None
    evidence_ids: tuple[str, ...] = ()


class MarketStructure(FrozenModel):
    version: Literal["completed-close-v1"] = "completed-close-v1"
    source: str
    price_basis: str
    windows: tuple[MarketWindow, ...]
    benchmark_status: Literal["UNKNOWN"] = "UNKNOWN"
    limitations: tuple[str, ...] = (
        "描述已提供的完成交易日收盘区间，不证明交易所日历完整；缺口不前填。",
        "不是支撑、阻力、底部或合理价值；区间位置可能在区间之外。",
        "不做股息总回报、行业 ETF 映射或不安全的复权比较。")


def market_structure(history: CloseHistory | None, symbol, price, cutoff, *, unknown_reason=None):
    cutoff = utc(cutoff)
    reason = unknown_reason or "HISTORY_UNAVAILABLE_OR_BASIS_UNVERIFIED"
    rows = ()
    if history:
        if history.symbol != symbol:
            raise ValueError("history symbol mismatch")
        rows = tuple(sorted(history.bars, key=lambda b:b.session_date))
        if len({b.session_date for b in rows}) != len(rows) or any(
            b.available_at < completed_session_end(b.session_date) for b in rows):
            raise ValueError("duplicate or premature completed-session history")
        rows = tuple(b for b in rows if b.available_at <= cutoff)
        if history.price_basis in {"UNADJUSTED_NO_SPLITS", "SPLIT_ADJUSTED"} and history.corporate_actions_verified and history.completeness == "PROVIDED_SESSIONS":
            reason = "STALE_COMPLETED_HISTORY" if rows and cutoff-rows[-1].available_at>timedelta(days=7) else None
    windows = []
    for count in (20, 60, 252):
        unavailable = reason or ("INSUFFICIENT_COMPLETED_SESSIONS" if len(rows)<count else None)
        if unavailable:
            windows.append(MarketWindow(sessions=count,status="UNKNOWN",reason=unavailable))
            continue
        selected = rows[-count:]
        high, low = max(b.close for b in selected), min(b.close for b in selected)
        windows.append(MarketWindow(sessions=count,status="AVAILABLE",start=selected[0].session_date,
            end=selected[-1].session_date,close_low=low,close_high=high,
            decision_price_range_position_pct=(price-low)*100/(high-low) if high!=low else None,
            decision_price_drawdown_from_close_high_pct=(price/high-1)*100,
            window_close_change_pct=(selected[-1].close/selected[0].close-1)*100,
            evidence_ids=tuple(dict.fromkeys((*history.source_evidence_ids, *(b.evidence_id for b in selected))))))
    return MarketStructure(source=history.source if history else "UNAVAILABLE",
        price_basis=history.price_basis if history else "UNVERIFIED",windows=tuple(windows))


class ReportedFact(FrozenModel):
    metric: str
    value: Finite
    unit: Literal["USD"]
    start: date | None
    end: date
    accession: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    accepted_at: datetime
    evidence_id: str
    _utc = field_validator("accepted_at")(utc)


class InvalidationCondition(FrozenModel):
    index: int
    text: str
    evidence_ids: tuple[str, ...]


class InvestmentContextPacket(FrozenModel):
    version: Literal["idc-v1"] = "idc-v1"
    intent: Intent
    symbol: str
    decision_price_usd: Positive
    decision_price_source: Literal["USER_INPUT", "COMPLETED_CLOSE"] = "USER_INPUT"
    scenario_cause: Cause | None
    assumptions: tuple[ScenarioAssumption, ...]
    market_reference_status: str
    market_reference_evidence_id: str | None
    market_structure: MarketStructure
    portfolio_constraints: tuple[PortfolioConstraint, ...]
    owner_policy: OwnerPolicy
    valuation_status: Literal["UNKNOWN", "REPORTED_FACTS_ONLY"]
    reported_facts: tuple[ReportedFact, ...]
    thesis_version_id: str | None
    invalidation_conditions: tuple[InvalidationCondition, ...]
    actual_company_evidence_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    cutoff: datetime
    missingness: tuple[str, ...]
    packet_hash: str
    _utc = field_validator("cutoff")(utc)

    @model_validator(mode="after")
    def integrity(self):
        if self.packet_hash != digest(self.model_dump(mode="json",exclude={"packet_hash"})):
            raise ValueError("investment context hash mismatch")
        ids = set(self.evidence_ids)
        if len(ids) != len(self.evidence_ids) or any(eid.startswith("A:") for eid in ids):
            raise ValueError("actual evidence namespace cannot contain assumptions")
        if set(self.actual_company_evidence_ids)-ids:
            raise ValueError("unknown actual company evidence")
        for fact in self.reported_facts:
            if fact.evidence_id not in self.actual_company_evidence_ids or fact.accepted_at > self.cutoff or fact.end>fact.accepted_at.date() or (fact.start and fact.start>fact.end):
                raise ValueError("reported fact PIT or period mismatch")
        refs = [eid for c in self.portfolio_constraints for eid in c.evidence_ids]
        refs += [eid for w in self.market_structure.windows for eid in w.evidence_ids]
        refs += [eid for c in self.invalidation_conditions for eid in c.evidence_ids]
        if set(refs)-ids or (self.market_reference_evidence_id and self.market_reference_evidence_id not in ids):
            raise ValueError("context lineage outside real evidence")
        for a in self.assumptions:
            if a.invalidation_condition_index is not None and a.invalidation_condition_index >= len(self.invalidation_conditions):
                raise ValueError("assumed invalidation requires existing named thesis condition")
        if self.intent == "CURRENT" and (self.assumptions or self.scenario_cause):
            raise ValueError("current context has hypothetical inputs")
        if self.intent == "HYPOTHETICAL" and (not self.scenario_cause or not self.assumptions):
            raise ValueError("hypothetical context needs assumptions")
        if any(c.as_of>self.cutoff for c in self.portfolio_constraints):
            raise ValueError("future constraint calculation")
        return self


class ConditionedStatement(FrozenModel):
    text: str = Field(min_length=1,max_length=500)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    assumption_ids: tuple[str, ...] = ()


class ConditionalAssessment(FrozenModel):
    intent: Intent
    headline: ConditionedStatement
    scenario_reasoning: ConditionedStatement
    binding_constraints: tuple[ConditionedStatement, ...] = Field(min_length=1,max_length=3)
    change_conditions: ConditionedStatement
    actual_thesis_state: Literal["UNKNOWN", "NOT_TRIGGERED", "INVALIDATION_SUPPORTED"]
    actual_invalidation_condition_index: int | None = Field(default=None,ge=0)
    actual_invalidation_evidence_ids: tuple[str, ...] = ()


def conditional_statements(assessment):
    return (assessment.headline,assessment.scenario_reasoning,*assessment.binding_constraints,assessment.change_conditions)


def validate_conditional(assessment, packet, thesis_delta=None):
    from .thesis import NUMBER_TOKEN
    if assessment is None or assessment.intent != packet.intent:
        raise ValueError("missing or mismatched conditional assessment")
    assumptions = {a.assumption_id for a in packet.assumptions}
    seen = set()
    for statement in conditional_statements(assessment):
        if set(statement.evidence_ids)-set(packet.evidence_ids) or set(statement.assumption_ids)-assumptions or NUMBER_TOKEN.search(statement.text):
            raise ValueError("conditional claim references or numeric prose invalid")
        seen.update(statement.assumption_ids)
    if packet.intent == "HYPOTHETICAL" and (seen != assumptions or not re.search(r"假设|若|如果|条件",assessment.headline.text)):
        raise ValueError("hypothetical judgment must surface all assumptions and conditional language")
    if packet.actual_company_evidence_ids and not set(assessment.scenario_reasoning.evidence_ids)&set(packet.actual_company_evidence_ids):
        raise ValueError("business scenario reasoning requires actual company grounding")
    triggered = assessment.actual_thesis_state == "INVALIDATION_SUPPORTED"
    if triggered != (assessment.actual_invalidation_condition_index is not None) or triggered != bool(assessment.actual_invalidation_evidence_ids):
        raise ValueError("actual invalidation linkage missing")
    if triggered and (assessment.actual_invalidation_condition_index >= len(packet.invalidation_conditions)
        or set(assessment.actual_invalidation_evidence_ids)-set(packet.actual_company_evidence_ids)):
        raise ValueError("actual invalidation needs named condition and eligible company evidence")

    if thesis_delta is not None and thesis_delta.impact in {"STRENGTHENED", "WEAKENED", "INVALIDATION_TRIGGERED"}:
        if not thesis_delta.evidence_ids or set(thesis_delta.evidence_ids)-set(packet.actual_company_evidence_ids):
            raise ValueError("actual thesis delta needs eligible company evidence")
        if thesis_delta.impact == "INVALIDATION_TRIGGERED" and (not triggered or
            thesis_delta.invalidation_condition_index != assessment.actual_invalidation_condition_index):
            raise ValueError("actual thesis invalidation channels disagree")


def reported_facts(public, cutoff):
    reported = []
    for item in public:
        if item.evidence_type != "sec_filing":
            continue
        for fact in (item.structured_data or {}).get("metrics",[]):
            try:
                row = ReportedFact(metric=fact["metric"],value=fact["value"],unit=fact["unit"],
                    start=fact.get("start"),end=fact["end"],accession=fact["accession_number"],
                    accepted_at=fact["available_at"],evidence_id=item.evidence_id)
            except (ValueError,KeyError,TypeError):
                continue
            if row.accepted_at<=cutoff and row.end<=row.accepted_at.date() and (not row.start or row.start<=row.end):
                reported.append(row)
    return tuple(reported)


def build_packet(request, context, snapshot, thesis, public, selected, *, cutoff, structure, nav_weight,
                 nav_numerator, nav_denominator, missing_symbols, valuation_evidence_ids):
    refs = tuple(dict.fromkeys((*context.input_evidence_ids,*valuation_evidence_ids)))
    costs = sum((p.quantity*p.avg_cost for p in snapshot.positions),Decimal(0))
    common = dict(as_of=cutoff,snapshot_id=snapshot.snapshot_id,snapshot_hash=snapshot.content_hash)
    constraints = (
        PortfolioConstraint(metric="POSITION_NAV_WEIGHT_PCT",value=nav_weight,
            numerator_usd=nav_numerator,denominator_usd=nav_denominator,
            numerator_semantics="Target shares × " + request.source + " decision price",
            denominator_semantics="Target scenario value + other eligible EOD equity + all account cash",
            basis="USER_PRICE_TARGET_PLUS_ELIGIBLE_EOD_OTHERS" if request.source == "USER_INPUT" else
                "COMPLETED_CLOSE_TARGET_PLUS_ELIGIBLE_EOD_OTHERS",status="KNOWN" if nav_weight is not None else "UNKNOWN",
            missing_symbols=tuple(missing_symbols),evidence_ids=refs,
            binding_to_explicit_policy=(nav_weight>request.owner_policy.single_name_nav_limit_pct
                if nav_weight is not None and request.owner_policy.single_name_nav_limit_pct is not None else None),**common),
        PortfolioConstraint(metric="POSITION_COST_EXPOSURE_PCT",value=context.portfolio_cost_exposure_pct,
            numerator_usd=context.cost_basis_usd,denominator_usd=costs or None,
            numerator_semantics="Target shares × imported average cost",
            denominator_semantics="All equity acquisition costs, excluding cash",
            basis="PORTFOLIO_COST",status="KNOWN" if costs else "UNKNOWN",evidence_ids=context.input_evidence_ids,**common))
    reported = reported_facts(public, cutoff)
    missing = ["VALUATION_UNKNOWN", "BENCHMARK_UNKNOWN"]
    if request.owner_policy.status == "NOT_CONFIGURED": missing.append("OWNER_POLICY_NOT_CONFIGURED")
    if context.market_reference is None: missing.append("MARKET_REFERENCE_UNAVAILABLE")
    if nav_weight is None: missing.append("PORTFOLIO_VALUATION_INCOMPLETE")
    if not reported: missing.append("REPORTED_FACTS_UNAVAILABLE")
    payload = dict(intent=request.intent,symbol=request.symbol,decision_price_usd=request.decision_price,
        decision_price_source=request.source,
        scenario_cause=request.scenario_cause,assumptions=request.assumptions,
        market_reference_status=context.market_reference_status,
        market_reference_evidence_id=context.market_reference.evidence_id if context.market_reference else None,
        market_structure=structure,portfolio_constraints=constraints,owner_policy=request.owner_policy,
        valuation_status="REPORTED_FACTS_ONLY" if reported else "UNKNOWN",reported_facts=tuple(reported),
        thesis_version_id=thesis.thesis_version_id if thesis else None,
        invalidation_conditions=tuple(InvalidationCondition(index=i,text=c.text,evidence_ids=tuple(c.evidence_ids))
            for i,c in enumerate(thesis.invalidation_conditions if thesis else [])),
        actual_company_evidence_ids=tuple(i.evidence_id for i in public if i.evidence_type in {"news","sec_filing"}),
        evidence_ids=tuple(i.evidence_id for i in selected),cutoff=cutoff,missingness=tuple(missing))
    obj = InvestmentContextPacket.model_construct(**payload)
    return InvestmentContextPacket(**payload,packet_hash=digest(obj.model_dump(mode="json",exclude={"packet_hash"})))
