"""Versioned, provider-neutral semantic decision contract."""

from decimal import Decimal, ROUND_DOWN
from typing import Literal, Annotated

from pydantic import Field, model_validator, model_serializer

from .domain import FrozenModel
from .investment_context import ConditionalAssessment


Confidence = Literal["HIGH", "MEDIUM", "LOW", "INSUFFICIENT"]
Action = Literal["ADD", "HOLD", "REDUCE", "EXIT"]


class ThesisDelta(FrozenModel):
    impact: Literal["STRENGTHENED", "WEAKENED", "UNCHANGED", "INVALIDATION_TRIGGERED", "INSUFFICIENT"]
    rationale: str = Field(min_length=1, max_length=500)
    evidence_ids: tuple[str, ...] = ()
    invalidation_condition_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def grounded(self):
        if self.impact != "INSUFFICIENT" and not self.evidence_ids:
            raise ValueError("material thesis delta requires evidence")
        if (self.impact == "INVALIDATION_TRIGGERED") != (self.invalidation_condition_index is not None):
            raise ValueError("invalidation claim must identify an existing condition")
        return self


class ActionRationale(FrozenModel):
    action: Action
    rationale: str = Field(min_length=1, max_length=400)
    evidence_ids: tuple[str, ...] = Field(min_length=1)


class SupportScores(FrozenModel):
    ADD: int = Field(ge=0, le=4)
    HOLD: int = Field(ge=0, le=4)
    REDUCE: int = Field(ge=0, le=4)
    EXIT: int = Field(ge=0, le=4)

    def values(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in ("ADD", "HOLD", "REDUCE", "EXIT")}


class DecisionAssessment(FrozenModel):
    contract_version: Literal[1] = 1
    assessment_status: Literal["ASSESSED", "RESEARCH_FIRST", "INSUFFICIENT_EVIDENCE"]
    scores: SupportScores | None = None
    rationales: tuple[ActionRationale, ...] = ()
    evidence_confidence: Confidence
    inference_confidence: Confidence
    supporting_evidence_ids: tuple[str, ...] = ()
    counter_evidence_ids: tuple[str, ...] = ()
    abstention_reason: str | None = Field(default=None, max_length=400)

    @model_validator(mode="after")
    def coherent(self):
        if self.assessment_status == "ASSESSED":
            if (self.scores is None or not any(self.scores.values().values())
                    or {item.action for item in self.rationales} != {"ADD", "HOLD", "REDUCE", "EXIT"}
                    or len(self.rationales) != 4 or not self.supporting_evidence_ids
                    or self.abstention_reason is not None):
                raise ValueError("assessed decision requires comparable grounded actions")
            if ("INSUFFICIENT" in {self.evidence_confidence, self.inference_confidence}
                    or (self.counter_evidence_ids and self.inference_confidence == "LOW")):
                raise ValueError("insufficient or high-conflict assessment must abstain")
        elif self.scores is not None or self.rationales or not self.abstention_reason:
            raise ValueError("abstention must have a reason and no action distribution")
        return self


class JudgeResult(FrozenModel):
    thesis_delta: ThesisDelta
    assessment: DecisionAssessment
    depth: "DecisionDepth | None" = None
    price_assessment: "PriceAssessment | None" = None
    conditional_assessment: ConditionalAssessment | None = None

    @model_serializer(mode="wrap")
    def legacy_result_shape(self,handler):
        payload=handler(self)
        if self.conditional_assessment is None:
            payload.pop("conditional_assessment",None)
        return payload


class EvidenceStatement(FrozenModel):
    text: str = Field(min_length=1, max_length=500)
    evidence_ids: tuple[str, ...] = Field(min_length=1)


class EvidenceGap(FrozenModel):
    dimension: Literal["COMPANY_SPECIFIC", "BUSINESS_PERFORMANCE", "VALUATION", "THESIS_CRITICAL"]
    description: str = Field(min_length=1, max_length=500)
    query: str = Field(min_length=1, max_length=200)


class ResearchCompletion(FrozenModel):
    initial_gaps: tuple[EvidenceGap, ...] = ()
    completion_attempted: bool = False
    supplemental_evidence_ids: tuple[str, ...] = ()
    remaining_gaps: tuple[EvidenceGap, ...] = ()
    provider_errors: tuple[str, ...] = ()


class DecisionDepth(FrozenModel):
    headline: EvidenceStatement
    baseline_thesis: EvidenceStatement
    price_implication: EvidenceStatement
    business_quality: EvidenceStatement
    key_risks: tuple[EvidenceStatement, ...] = Field(min_length=1, max_length=3)
    change_my_mind: tuple[EvidenceStatement, ...] = Field(min_length=1, max_length=3)
    next_verification: EvidenceStatement
    missing_context: tuple[Annotated[str, Field(min_length=1, max_length=300)], ...] = Field(max_length=5)


class PriceActionEffect(FrozenModel):
    action: Action
    direction: Literal["FAVORS", "DISFAVORS", "NOT_DECISIVE", "UNKNOWN"]
    strength: Literal["LIMITED", "MEANINGFUL", "NONE", "UNKNOWN"]


class PriceAssessment(FrozenModel):
    """Price-conditioned action tradeoffs, distinct from business/thesis change.

    Direction/strength are qualitative judgments against the named reference,
    not a buy rule, probability, valuation model, or investment threshold.
    """
    version: Literal[1] = 1
    reference_basis: Literal["CURRENT_REFERENCE", "PREVIOUS_CLOSE", "UNAVAILABLE"]
    sensitivity: Literal["PRICE_RELEVANT", "CONSTRAINT_DOMINATED", "UNKNOWN"]
    effects: tuple[PriceActionEffect, ...] = Field(min_length=4, max_length=4)
    invariance_rationale: EvidenceStatement | None = None

    @model_validator(mode="after")
    def complete(self):
        if {effect.action for effect in self.effects} != {"ADD", "HOLD", "REDUCE", "EXIT"}:
            raise ValueError("price assessment requires each action exactly once")
        if self.sensitivity == "CONSTRAINT_DOMINATED" and self.invariance_rationale is None:
            raise ValueError("unchanged action balance requires a grounded invariance rationale")
        if self.reference_basis == "UNAVAILABLE" and any(effect.direction != "UNKNOWN" for effect in self.effects):
            raise ValueError("unknown reference cannot support a relative price effect")
        if any(effect.direction == "UNKNOWN" and effect.strength != "UNKNOWN" for effect in self.effects):
            raise ValueError("unknown price effect requires unknown strength")
        return self


class JudgeDraftRationale(ActionRationale):
    rationale: str = Field(min_length=1, max_length=200)


class JudgeDraftAssessment(FrozenModel):
    """Provider response before deterministic score and coherence validation."""

    contract_version: Literal[1] = 1
    assessment_status: Literal["ASSESSED", "RESEARCH_FIRST", "INSUFFICIENT_EVIDENCE"]
    scores: dict[str, object] | None = None
    rationales: tuple[JudgeDraftRationale, ...] = ()
    evidence_confidence: Confidence
    inference_confidence: Confidence
    supporting_evidence_ids: tuple[str, ...] = ()
    counter_evidence_ids: tuple[str, ...] = ()
    abstention_reason: str | None = Field(default=None, max_length=400)


class JudgeDraft(FrozenModel):
    price_effect: Literal["INCREASES_ATTRACTIVENESS", "DECREASES_ATTRACTIVENESS",
                          "NOT_DETERMINATIVE", "UNKNOWN"]
    price_effect_reason: str | None = Field(default=None, max_length=150)
    thesis_delta: ThesisDelta
    assessment: JudgeDraftAssessment


class OwnerJudgeDraft(JudgeDraft):
    """v5 decision depth is mandatory in production; v1-v4 remain readable."""
    depth: DecisionDepth


class PriceJudgeDraft(OwnerJudgeDraft):
    """v7 retains the score scale; adds mandatory price-conditioned assessment."""
    price_assessment: PriceAssessment


class ContextJudgeDraft(PriceJudgeDraft):
    conditional_assessment: ConditionalAssessment


class ActionSupport(FrozenModel):
    version: Literal[1] = 1
    label: Literal["Relative Semantic Action Support / 建议支持度"] = "Relative Semantic Action Support / 建议支持度"
    ADD: Decimal
    HOLD: Decimal
    REDUCE: Decimal
    EXIT: Decimal

    @model_validator(mode="after")
    def sums_to_hundred(self):
        if any(getattr(self, key) < 0 for key in ("ADD", "HOLD", "REDUCE", "EXIT")):
            raise ValueError("negative action support")
        if sum(getattr(self, key) for key in ("ADD", "HOLD", "REDUCE", "EXIT")) != 100:
            raise ValueError("action support must sum to 100")
        return self


def normalize_support(scores: SupportScores) -> ActionSupport:
    """Largest-remainder allocation in integer percentage points, with fixed tie order."""
    values = scores.values()
    total = sum(values.values())
    if total <= 0:
        raise ValueError("no meaningful action comparison")
    exact = {key: Decimal(value) * 100 / total for key, value in values.items()}
    whole = {key: int(value.to_integral_value(rounding=ROUND_DOWN)) for key, value in exact.items()}
    order = sorted(values, key=lambda key: (-(exact[key] - whole[key]), list(values).index(key)))
    for key in order[:100 - sum(whole.values())]:
        whole[key] += 1
    return ActionSupport(**whole)


class SemanticArtifact(FrozenModel):
    version: Literal[1, 2] = 1
    foundation_record_id: str
    foundation_run_id: str
    evidence_scope: Literal["CURRENT_30_DAY_NEWS_PLUS_SEC", "HISTORICAL_PERSISTED", "NO_THESIS"]
    judge_context_hash: str | None
    what_changed_mode: Literal["FIRST_REVIEW", "DELTA_REVIEW"]
    current_evidence_ids: tuple[str, ...]
    new_evidence_ids: tuple[str, ...]
    retained_counter_evidence_ids: tuple[str, ...] = ()
    thesis_delta: ThesisDelta
    assessment: DecisionAssessment
    action_support: ActionSupport | None
    explanation: str
    model_name: str | None
    prompt_contract: Literal["tdb-decision-judge-v1", "tdb-decision-judge-v2",
                             "tdb-decision-judge-v3", "tdb-decision-judge-v4", "tdb-decision-judge-v5",
                             "tdb-decision-judge-v6", "tdb-decision-judge-v7", "tdb-decision-judge-v8-idc"] = "tdb-decision-judge-v1"
    depth: DecisionDepth | None = None
    context_evidence_id: str | None = None
    research: ResearchCompletion | None = None
    price_assessment: PriceAssessment | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_price_field_absence(self, handler):
        # Hash validation and API round trips must preserve pre-v7 payload shape.
        # An explicitly added null is an edit, so only absent fields are omitted.
        payload = handler(self)
        if (self.price_assessment is None and "price_assessment" not in self.model_fields_set
                and self.prompt_contract != "tdb-decision-judge-v7"):
            payload.pop("price_assessment", None)
        return payload

    @model_validator(mode="after")
    def status_matches_support(self):
        if (self.assessment.assessment_status == "ASSESSED") != (self.action_support is not None):
            raise ValueError("action support and assessment status disagree")
        if set(self.new_evidence_ids) - set(self.current_evidence_ids):
            raise ValueError("new evidence must be current evidence")
        if self.what_changed_mode == "FIRST_REVIEW" and self.new_evidence_ids:
            raise ValueError("first review cannot claim historical novelty")
        return self
