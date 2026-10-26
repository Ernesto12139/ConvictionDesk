"""Typed LLM/Jev thesis-impact judgments and reproducible comparison."""

import hashlib
import json
from contextvars import ContextVar
from datetime import datetime
from enum import Enum
from time import perf_counter

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .clock import Clock, system_utc_now
from .domain import EvidenceItem, FrozenModel, utc
from .llm import LLMClient, LLMError, StructuredLLMRequest
from .thesis import ThesisProfile
from .thesis import NUMBER_TOKEN


JEV_BASE_URL = "https://api.typesafe.ai"


class EventMateriality(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    INSUFFICIENT = "insufficient"


class SemanticDirection(str, Enum):
    STRENGTHENS = "strengthens"
    WEAKENS = "weakens"
    MIXED = "mixed"
    NEUTRAL = "neutral"
    INSUFFICIENT = "insufficient"


class Persistence(str, Enum):
    TRANSIENT = "transient"
    UNCERTAIN = "uncertain"
    PERSISTENT = "persistent"
    INSUFFICIENT = "insufficient"


class ThesisImpact(str, Enum):
    STRENGTHENS = "strengthens"
    WEAKENS = "weakens"
    MIXED = "mixed"
    NO_MATERIAL_CHANGE = "no_material_change"
    INSUFFICIENT = "insufficient"


class ExecutionRisk(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    INSUFFICIENT = "insufficient"


class ConfidenceBand(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ComponentImpactDirection(str, Enum):
    STRENGTHENS = "strengthens"
    WEAKENS = "weakens"
    MIXED = "mixed"
    NOT_AFFECTED = "not_affected"
    INSUFFICIENT = "insufficient"


CHOICE_CRITERIA = {
    "event_materiality": {
        "none": "Unrelated to every material thesis component.",
        "low": "Peripheral evidence with little bearing on a thesis component.",
        "medium": "Meaningful evidence for at least one thesis component, but not thesis-breaking alone.",
        "high": "Directly affects a core driver, key assumption, or invalidation condition.",
        "insufficient": "The supplied evidence is not enough to judge materiality.",
    },
    "direction": {
        "strengthens": "Evidence is favorable to the supplied thesis on balance.",
        "weakens": "Evidence is unfavorable to the supplied thesis on balance.",
        "mixed": "Material favorable and unfavorable effects coexist.",
        "neutral": "Evidence has no directional effect on the thesis.",
        "insufficient": "The supplied evidence is not enough to judge direction.",
    },
    "persistence": {
        "transient": "Likely short-lived or one-off within the supplied record.",
        "uncertain": "Duration cannot be established from the supplied evidence.",
        "persistent": "Evidence supports an ongoing or structural effect.",
        "insufficient": "The supplied evidence is not enough to judge persistence.",
    },
    "thesis_impact": {
        "strengthens": "Raises support for one or more material thesis components.",
        "weakens": "Reduces support for one or more material thesis components.",
        "mixed": "Strengthens some material components while weakening others.",
        "no_material_change": "Does not materially change the supplied thesis.",
        "insufficient": "The supplied evidence is not enough to assess thesis impact.",
    },
    "execution_risk": {
        "none": "No execution-risk signal appears in the supplied evidence.",
        "low": "A limited execution concern with little present thesis impact.",
        "medium": "A meaningful execution concern that warrants monitoring.",
        "high": "A direct, material threat to delivery of a core thesis component.",
        "insufficient": "The supplied evidence is not enough to assess execution risk.",
    },
}

COMPONENT_IMPACT_CRITERIA = {
    "strengthens": "The evidence increases support for this specific thesis component.",
    "weakens": "The evidence reduces support for this specific thesis component.",
    "mixed": "The evidence both strengthens and weakens this specific component.",
    "not_affected": "The evidence does not bear materially on this specific component.",
    "insufficient": "The supplied evidence is not enough to judge this component.",
}


class ThesisComponentImpact(FrozenModel):
    component_type: str
    component_index: int = Field(ge=0)
    impact: ComponentImpactDirection
    evidence_ids: list[str] = Field(min_length=1)


class SemanticOutput(FrozenModel):
    event_materiality: EventMateriality
    direction: SemanticDirection
    persistence: Persistence
    thesis_impact: ThesisImpact
    execution_risk: ExecutionRisk
    confidence: ConfidenceBand
    evidence_ids: list[str] = Field(min_length=1)
    supporting_evidence_ids: list[str] = []
    counter_evidence_ids: list[str] = []
    component_impacts: list[ThesisComponentImpact] = Field(min_length=1)
    limitations: list[str] = []

    @model_validator(mode="after")
    def supporting_ids_are_declared(self):
        declared = set(self.evidence_ids)
        if (
            set(self.supporting_evidence_ids) - declared
            or set(self.counter_evidence_ids) - declared
            or any(set(item.evidence_ids) - declared for item in self.component_impacts)
        ):
            raise ValueError("supporting/counter evidence must be declared")
        return self


class SemanticAssessment(SemanticOutput):
    judge: str
    model: str
    created_at: datetime

    _created_utc = field_validator("created_at")(utc)


class SemanticJudgeResult(FrozenModel):
    assessment: SemanticAssessment
    latency_ms: int
    evidence: EvidenceItem | None = None


def semantic_state(thesis: ThesisProfile, evidence: list[EvidenceItem]) -> dict:
    return {
        "symbol": thesis.symbol,
        "thesis": thesis.model_dump(mode="json"),
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "evidence_type": item.evidence_type,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "title": item.title,
                "excerpt": item.excerpt,
            }
            for item in evidence
        ],
        "semantic_contract": CHOICE_CRITERIA,
        "component_impact_contract": COMPONENT_IMPACT_CRITERIA,
    }


def _thesis_components(thesis: ThesisProfile) -> list[tuple[str, int, object]]:
    return [
        (component_type, index, statement)
        for component_type, statements in (
            ("core_driver", thesis.core_drivers),
            ("key_assumption", thesis.key_assumptions),
            ("execution_risk", thesis.execution_risks),
            ("invalidation_condition", thesis.invalidation_conditions),
        )
        for index, statement in enumerate(statements)
    ]


def _default_output(thesis: ThesisProfile, evidence: list[EvidenceItem]) -> SemanticOutput:
    ids = [item.evidence_id for item in evidence]
    return SemanticOutput(
        event_materiality=EventMateriality.MEDIUM,
        direction=SemanticDirection.NEUTRAL,
        persistence=Persistence.UNCERTAIN,
        thesis_impact=ThesisImpact.NO_MATERIAL_CHANGE,
        execution_risk=ExecutionRisk.MEDIUM,
        confidence=ConfidenceBand.LOW,
        evidence_ids=ids,
        supporting_evidence_ids=ids,
        component_impacts=[
            ThesisComponentImpact(
                component_type=component_type,
                component_index=index,
                impact=ComponentImpactDirection.NOT_AFFECTED,
                evidence_ids=ids,
            )
            for component_type, index, _ in _thesis_components(thesis)
        ],
        limitations=["判断把握仅针对本次提供的证据，不代表未来收益概率。"],
    )


def _validate_ids(output: SemanticOutput, evidence: list[EvidenceItem]) -> None:
    allowed = {item.evidence_id for item in evidence}
    referenced = (
        set(output.evidence_ids)
        | set(output.supporting_evidence_ids)
        | set(output.counter_evidence_ids)
        | {
            evidence_id
            for component in output.component_impacts
            for evidence_id in component.evidence_ids
        }
    )
    if referenced - allowed:
        raise LLMError(
            "invalid_evidence_reference", "semantic judgment cites evidence outside the supplied set"
        )


def _validate_components(output: SemanticOutput, thesis: ThesisProfile) -> None:
    expected = {
        (component_type, index)
        for component_type, index, _ in _thesis_components(thesis)
    }
    actual = {
        (component.component_type, component.component_index)
        for component in output.component_impacts
    }
    if actual != expected or len(actual) != len(output.component_impacts):
        raise LLMError(
            "invalid_component_reference",
            "semantic judgment must cover every thesis component exactly once",
        )


def _semantic_evidence(
    assessment: SemanticAssessment, thesis: ThesisProfile, source: list[EvidenceItem]
) -> EvidenceItem:
    payload = {
        "assessment": assessment.model_dump(mode="json"),
        "thesis_version_id": thesis.thesis_version_id,
        "input_evidence_ids": [item.evidence_id for item in source],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return EvidenceItem(
        evidence_id="E" + digest[7:23],
        evidence_type="semantic_judgment",
        provider=assessment.judge,
        symbol=source[0].symbol if source else None,
        title=f"{assessment.judge} semantic thesis-impact assessment",
        source_record_id=assessment.model,
        retrieved_at=assessment.created_at,
        as_of=max(
            [thesis.as_of]
            + [item.published_at for item in source if item.published_at is not None]
        ),
        content_hash=digest,
        structured_data=payload,
    )


class LLMSemanticJudge:
    def __init__(self, llm: LLMClient, *, clock: Clock | None = None):
        self._validation_attempts = ContextVar("semantic_validation_attempts", default=[])
        self.llm = llm
        self.model_name = llm.model_name
        self.clock = clock or system_utc_now

    @property
    def provider(self):
        return getattr(self.llm, "provider", None)

    def call_metadata(self):
        return {**getattr(self.llm, "call_metadata", lambda: {})(),
                "validation_attempts": self._validation_attempts.get()}

    async def assess(
        self, thesis: ThesisProfile, evidence: list[EvidenceItem]
    ) -> SemanticJudgeResult:
        if not evidence:
            raise ValueError("INSUFFICIENT_EVIDENCE: semantic assessment requires event evidence")
        ids = [item.evidence_id for item in evidence]
        mock = _default_output(thesis, evidence)
        state = semantic_state(thesis, evidence)
        numeric_tokens = sorted(set(NUMBER_TOKEN.findall(json.dumps(state, ensure_ascii=False))))
        request = StructuredLLMRequest(
            purpose="thesis_impact_semantic_assessment",
            response_model=SemanticOutput,
            facts=state,
            evidence_ids=ids,
            numeric_fact_tokens=numeric_tokens,
            grounded_responses=[mock.model_dump(mode="json")],
            generation_mode="generate",
            system_instruction=(
                "Judge only the supplied event evidence against the supplied AI_DRAFT thesis. "
                "Use the enum semantics in the response schema, cite only allowed evidence IDs, "
                "do not calculate finance metrics, predict returns, browse, or emit trade actions. "
                "Cover every supplied thesis statement exactly once in component_impacts. "
                "Use zero-based indices within each group: core_drivers -> core_driver, "
                "key_assumptions -> key_assumption, execution_risks -> execution_risk, "
                "invalidation_conditions -> invalidation_condition. Never omit unaffected "
                "or insufficient components; assign their appropriate impact category. "
                "Write all owner-facing rationale and limitation prose in natural simplified Chinese; "
                "preserve source titles and company/product proper names where appropriate. "
                "Return only JSON matching the response schema."
            ),
        )
        started = perf_counter()
        self._validation_attempts.set([])
        for attempt in ("initial", "repair"):
            try:
                output = await self.llm.complete_structured(request)
                if not isinstance(output, SemanticOutput):
                    raise LLMError("schema_validation", "wrong semantic result type")
                _validate_ids(output, evidence)
                _validate_components(output, thesis)
                output_tokens = set(NUMBER_TOKEN.findall(" ".join(output.limitations)))
                if output_tokens - set(numeric_tokens):
                    raise LLMError("unsupported_numeric_fact", "unsupported semantic number")
            except LLMError as exc:
                self._validation_attempts.set([*self._validation_attempts.get(), {"attempt": attempt, "error": exc.code}])
                if attempt == "repair" or exc.code not in {"schema_validation", "malformed_json", "invalid_component_reference"}:
                    raise
                request = request.model_copy(update={"system_instruction": request.system_instruction +
                    " Repair the rejected structure using the same frozen thesis/evidence. Error: " + str(exc)[:300]})
            else:
                self._validation_attempts.set([*self._validation_attempts.get(), {"attempt": attempt, "error": None}])
                break
        latency_ms = max(0, round((perf_counter() - started) * 1000))
        if not isinstance(output, SemanticOutput):
            raise LLMError("schema_validation", "LLM returned the wrong semantic result type")
        _validate_ids(output, evidence)
        _validate_components(output, thesis)
        output_tokens = set(NUMBER_TOKEN.findall(" ".join(output.limitations)))
        if output_tokens - set(numeric_tokens):
            raise LLMError(
                "unsupported_numeric_fact",
                "semantic judgment introduced a numeric fact absent from supplied state",
            )
        assessment = SemanticAssessment(
            judge="llm",
            model=self.llm.model_name,
            created_at=utc(self.clock()),
            **output.model_dump(),
        )
        return SemanticJudgeResult(
            assessment=assessment,
            latency_ms=latency_ms,
            evidence=_semantic_evidence(assessment, thesis, evidence),
        )


class SemanticJudgeError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class _ChoiceAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    choice: str
    probabilities: dict[str, float]
    confidence: float = Field(ge=0, le=1)


class _NoulAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    noul: float = Field(ge=0, le=1)


class _JevResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    answers: dict[str, dict]
    usage: dict


def _valid_choice_distribution(answer: _ChoiceAnswer, criteria: dict[str, str]) -> bool:
    probabilities = answer.probabilities
    return (
        set(probabilities) == set(criteria)
        and all(0 <= probability <= 1 for probability in probabilities.values())
        and abs(sum(probabilities.values()) - 1.0) <= 1e-6
        and probabilities[answer.choice] == max(probabilities.values())
    )


class JevSemanticJudge:
    provider = "jev"
    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Clock | None = None,
        base_url: str = JEV_BASE_URL,
        model: str = "jev-latest",
        timeout: float = 30.0,
        _offline_test=False,
    ):
        self._api_key = api_key
        self.client = client
        self.clock = clock or system_utc_now
        self.base_url = base_url.rstrip("/")
        self._offline_test = _offline_test
        self.model = model
        self.model_name = model
        self.timeout = timeout
        self._metadata: ContextVar[dict] = ContextVar("jev_call_metadata", default={})

    def call_metadata(self):
        return self._metadata.get()

    def _admit(self, payload, client):
        from .research_egress import admit_model_send
        admit_model_send('jev',self.model,self.base_url+'/v1/systemone',payload['state'],
            client=client,offline_test=self._offline_test)

    async def assess(
        self, thesis: ThesisProfile, evidence: list[EvidenceItem]
    ) -> SemanticJudgeResult:
        self._metadata.set({})
        if not self._api_key:
            raise SemanticJudgeError("configuration", "JEV_API_KEY is not configured")
        if not evidence:
            raise ValueError("INSUFFICIENT_EVIDENCE: semantic assessment requires event evidence")
        questions = {
            key: {
                "type": "choice",
                "instructions": f"Classify {key.replace('_', ' ')} using only the supplied state.",
                "criteria": criteria,
            }
            for key, criteria in CHOICE_CRITERIA.items()
        }
        for index, _ in enumerate(evidence):
            questions[f"supports_{index}"] = {
                "type": "noul",
                "instructions": f"Does evidence item {index} support the supplied thesis?",
            }
            questions[f"counters_{index}"] = {
                "type": "noul",
                "instructions": f"Does evidence item {index} counter the supplied thesis?",
            }
        components = _thesis_components(thesis)
        for index, (component_type, component_index, statement) in enumerate(components):
            questions[f"component_impact_{index}"] = {
                "type": "choice",
                "instructions": {
                    "question": "How does the supplied evidence affect this thesis component?",
                    "component_type": component_type,
                    "component_index": component_index,
                    "component_text": statement.text,
                },
                "criteria": COMPONENT_IMPACT_CRITERIA,
            }
        payload = {
            "state": semantic_state(thesis, evidence),
            "model": self.model,
            "questions": questions,
        }
        started = perf_counter()
        try:
            if self.client is None:
                async with httpx.AsyncClient() as client:
                    self._admit(payload,client)
                    response = await client.post(
                        f"{self.base_url}/v1/systemone",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json=payload,
                        timeout=self.timeout, follow_redirects=False,
                    )
            else:
                self._admit(payload,self.client)
                response = await self.client.post(
                    f"{self.base_url}/v1/systemone",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                    timeout=self.timeout, follow_redirects=False,
                )
        except LLMError as exc:
            raise SemanticJudgeError(exc.code, "Jev model admission refused") from None
        except httpx.TimeoutException:
            raise SemanticJudgeError("timeout", "Jev request timed out") from None
        except httpx.RequestError:
            raise SemanticJudgeError("transport", "Jev transport failure") from None
        latency_ms = max(0, round((perf_counter() - started) * 1000))
        if response.status_code in (401, 403):
            raise SemanticJudgeError("authentication", f"Jev authentication failed ({response.status_code})")
        if response.status_code == 422:
            raise SemanticJudgeError("invalid_request", "Jev rejected the typed request (422)")
        if response.status_code == 429:
            raise SemanticJudgeError("rate_limit", "Jev rate limit exceeded (429)")
        if response.status_code == 529 or response.status_code >= 500:
            raise SemanticJudgeError("retryable", f"Jev unavailable ({response.status_code})")
        if response.status_code != 200:
            raise SemanticJudgeError("provider", f"Jev request failed ({response.status_code})")
        try:
            envelope = _JevResponse.model_validate(response.json())
            self._metadata.set({"latency_ms": latency_ms,
                "usage": {key: envelope.usage[key] for key in ("input_tokens", "output_tokens")
                          if type(envelope.usage.get(key)) is int and envelope.usage[key] >= 0}})
            choices = {
                key: _ChoiceAnswer.model_validate(envelope.answers[key])
                for key in CHOICE_CRITERIA
            }
            support = [
                _NoulAnswer.model_validate(envelope.answers[f"supports_{index}"])
                for index in range(len(evidence))
            ]
            counter = [
                _NoulAnswer.model_validate(envelope.answers[f"counters_{index}"])
                for index in range(len(evidence))
            ]
            component_answers = [
                _ChoiceAnswer.model_validate(envelope.answers[f"component_impact_{index}"])
                for index in range(len(components))
            ]
        except (ValueError, KeyError, TypeError):
            raise SemanticJudgeError("invalid_data", "Jev returned invalid typed output") from None
        for key, answer in choices.items():
            if (
                answer.type != "choice"
                or answer.choice not in CHOICE_CRITERIA[key]
                or not _valid_choice_distribution(answer, CHOICE_CRITERIA[key])
            ):
                raise SemanticJudgeError("invalid_data", "Jev returned an invalid choice")
        if any(answer.type != "noul" for answer in [*support, *counter]):
            raise SemanticJudgeError("invalid_data", "Jev returned an invalid Noul")
        if any(
            answer.type != "choice"
            or answer.choice not in COMPONENT_IMPACT_CRITERIA
            or not _valid_choice_distribution(answer, COMPONENT_IMPACT_CRITERIA)
            for answer in component_answers
        ):
            raise SemanticJudgeError("invalid_data", "Jev returned an invalid component choice")
        minimum_confidence = min(
            answer.confidence for answer in [*choices.values(), *component_answers]
        )
        confidence = (
            ConfidenceBand.HIGH if minimum_confidence >= 0.8
            else ConfidenceBand.MEDIUM if minimum_confidence >= 0.55
            else ConfidenceBand.LOW
        )
        ids = [item.evidence_id for item in evidence]
        output = SemanticOutput(
            event_materiality=choices["event_materiality"].choice,
            direction=choices["direction"].choice,
            persistence=choices["persistence"].choice,
            thesis_impact=choices["thesis_impact"].choice,
            execution_risk=choices["execution_risk"].choice,
            confidence=confidence,
            evidence_ids=ids,
            supporting_evidence_ids=[item.evidence_id for item, answer in zip(evidence, support, strict=True) if answer.noul >= 0.6],
            counter_evidence_ids=[item.evidence_id for item, answer in zip(evidence, counter, strict=True) if answer.noul >= 0.6],
            component_impacts=[
                ThesisComponentImpact(
                    component_type=component_type,
                    component_index=component_index,
                    impact=answer.choice,
                    evidence_ids=ids,
                )
                for (component_type, component_index, _), answer in zip(
                    components, component_answers, strict=True
                )
            ],
            limitations=["Jev 仅对本次提供的投资逻辑与证据进行语义对照。"],
        )
        assessment = SemanticAssessment(
            judge="jev", model=envelope.model, created_at=utc(self.clock()), **output.model_dump()
        )
        return SemanticJudgeResult(
            assessment=assessment,
            latency_ms=latency_ms,
            evidence=_semantic_evidence(assessment, thesis, evidence),
        )


class JudgeBenchmark(FrozenModel):
    judge: str
    model: str
    latency_ms: int
    structured_output_valid: bool
    evidence_ids_valid: bool
    assessment: SemanticAssessment


class SemanticBenchmarkResult(FrozenModel):
    input_hash: str
    evidence_ids: list[str]
    llm: JudgeBenchmark
    jev: JudgeBenchmark
    disagreements: list[str]


async def run_semantic_benchmark(
    thesis: ThesisProfile, evidence: list[EvidenceItem], llm_judge, jev_judge
) -> SemanticBenchmarkResult:
    llm_result = await llm_judge.assess(thesis, evidence)
    jev_result = await jev_judge.assess(thesis, evidence)
    return semantic_benchmark_from_results(thesis, evidence, llm_result, jev_result)


def semantic_benchmark_from_results(
    thesis: ThesisProfile,
    evidence: list[EvidenceItem],
    llm_result: SemanticJudgeResult,
    jev_result: SemanticJudgeResult,
) -> SemanticBenchmarkResult:
    state = semantic_state(thesis, evidence)
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    input_hash = "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    allowed = {item.evidence_id for item in evidence}

    def record(result: SemanticJudgeResult) -> JudgeBenchmark:
        return JudgeBenchmark(
            judge=result.assessment.judge,
            model=result.assessment.model,
            latency_ms=result.latency_ms,
            structured_output_valid=True,
            evidence_ids_valid=set(result.assessment.evidence_ids) <= allowed,
            assessment=result.assessment,
        )

    fields = (
        "event_materiality", "direction", "persistence", "thesis_impact",
        "execution_risk", "confidence",
    )
    disagreements = [
        field for field in fields
        if getattr(llm_result.assessment, field) != getattr(jev_result.assessment, field)
    ]
    return SemanticBenchmarkResult(
        input_hash=input_hash,
        evidence_ids=[item.evidence_id for item in evidence],
        llm=record(llm_result),
        jev=record(jev_result),
        disagreements=disagreements,
    )
