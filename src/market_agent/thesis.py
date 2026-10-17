"""Typed, versioned, evidence-grounded AI thesis drafts."""

import hashlib
import json
import re
from datetime import datetime
from enum import Enum

from pydantic import Field, field_validator, model_validator

from .clock import Clock, system_utc_now
from .domain import EvidenceItem, FrozenModel, utc
from .llm import LLMClient, LLMError, StructuredLLMRequest
from .persistence import Store


NUMBER_TOKEN = re.compile(
    r"(?<!\w)[-+]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)"
    r"(?:[eE][+-]?\d+)?%?(?!\w)"
)
WORD_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9-]{3,}")
NEGATION_TOKEN = re.compile(
    r"\b(?:no|not|never|without|neither|nor|cannot|can't|isn't|aren't|wasn't|"
    r"weren't|doesn't|don't|didn't|won't|wouldn't|shouldn't|couldn't|hasn't|"
    r"haven't|hadn't)\b",
    re.IGNORECASE,
)
ALLOWED_THESIS_WORDS = {
    "about", "against", "assumption", "business", "cited", "condition",
    "contradicting", "contradictory", "core", "direction", "draft", "driver",
    "evidence", "execution", "future", "identifies", "invalidate", "invalidation",
    "later", "limited", "logic", "material", "observed", "public", "recent", "record",
    "reflected", "relevant", "remains", "require", "requires", "revising", "risk",
    "stated", "statement", "supplied", "support", "supports", "thesis", "this",
    "uncertain", "would",
}


def _hash(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ThesisStatus(str, Enum):
    AI_DRAFT = "AI_DRAFT"
    USER_CONFIRMED = "USER_CONFIRMED"


class ThesisStatement(FrozenModel):
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class ThesisDraftOutput(FrozenModel):
    core_drivers: list[ThesisStatement] = Field(min_length=1)
    key_assumptions: list[ThesisStatement] = Field(min_length=1)
    execution_risks: list[ThesisStatement] = Field(min_length=1)
    invalidation_conditions: list[ThesisStatement] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def statement_ids_are_declared(self):
        declared = set(self.evidence_ids)
        referenced = {
            evidence_id
            for group in (
                self.core_drivers,
                self.key_assumptions,
                self.execution_risks,
                self.invalidation_conditions,
            )
            for statement in group
            for evidence_id in statement.evidence_ids
        }
        if referenced - declared:
            raise ValueError("thesis statement cites evidence absent from thesis evidence_ids")
        return self


class ThesisGenerationMetadata(FrozenModel):
    model: str
    purpose: str = "ai_thesis_draft"


class ThesisProfile(ThesisDraftOutput):
    symbol: str
    thesis_version_id: str
    as_of: datetime
    status: ThesisStatus
    generation: ThesisGenerationMetadata

    _as_of_utc = field_validator("as_of")(utc)


def _evidence_facts(items: list[EvidenceItem]) -> list[dict]:
    return [
        {
            "evidence_id": item.evidence_id,
            "evidence_type": item.evidence_type,
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "title": item.title,
            "excerpt": item.excerpt,
        }
        for item in items
    ]


def _text_tokens(items: list[EvidenceItem]) -> set[str]:
    source = " ".join(
        part
        for item in items
        for part in (item.title, item.excerpt)
        if part
    )
    return set(NUMBER_TOKEN.findall(source))


def _output_tokens(output: ThesisDraftOutput) -> set[str]:
    return {
        token
        for group in (
            output.core_drivers,
            output.key_assumptions,
            output.execution_risks,
            output.invalidation_conditions,
        )
        for statement in group
        for token in NUMBER_TOKEN.findall(statement.text)
    }


def _material_terms(text: str) -> set[str]:
    return {
        token.lower()
        for token in (*WORD_TOKEN.findall(text), *NEGATION_TOKEN.findall(text))
    }


def _validate_qualitative_support(
    output: ThesisDraftOutput, evidence: list[EvidenceItem]
) -> None:
    evidence_by_id = {item.evidence_id: item for item in evidence}
    for group in (
        output.core_drivers,
        output.key_assumptions,
        output.execution_risks,
        output.invalidation_conditions,
    ):
        for statement in group:
            cited_terms = {
                term
                for evidence_id in statement.evidence_ids
                for term in _material_terms(
                    " ".join(
                        part
                        for part in (
                            evidence_by_id[evidence_id].title,
                            evidence_by_id[evidence_id].excerpt,
                        )
                        if part
                    )
                )
            }
            statement_terms = _material_terms(statement.text)
            if cited_terms and (
                not (statement_terms & cited_terms)
                or statement_terms - cited_terms - ALLOWED_THESIS_WORDS
            ):
                raise LLMError(
                    "unsupported_claim",
                    "thesis statement introduced content terms absent from cited evidence",
                )


def _deterministic_mock_output(items: list[EvidenceItem]) -> ThesisDraftOutput:
    first = items[0]
    source = first.title or first.excerpt or "公开证据"
    return ThesisDraftOutput(
        core_drivers=[ThesisStatement(
            text=f"公开材料反映的核心驱动：{source}",
            evidence_ids=[first.evidence_id],
        )],
        key_assumptions=[ThesisStatement(
            text=f"关键假设：{source} 所反映的业务方向继续成立。",
            evidence_ids=[first.evidence_id],
        )],
        execution_risks=[ThesisStatement(
            text=f"执行风险：{source} 的支持范围仍限于本次公开材料。",
            evidence_ids=[first.evidence_id],
        )],
        invalidation_conditions=[ThesisStatement(
            text=f"失效条件：后续证据若推翻 {source}，需要复核本草稿。",
            evidence_ids=[first.evidence_id],
        )],
        evidence_ids=[first.evidence_id],
    )


class AIThesisGenerator:
    def __init__(self, store: Store, llm: LLMClient, *, clock: Clock | None = None):
        self.store = store
        self.llm = llm
        self.clock = clock or system_utc_now

    async def generate(
        self,
        symbol: str,
        evidence: list[EvidenceItem],
        as_of: datetime,
        *,
        persist: bool = True,
        conservative: bool = False,
    ) -> tuple[ThesisProfile, EvidenceItem]:
        cutoff = utc(as_of)
        eligible = [
            item for item in evidence
            if item.symbol == symbol and item.published_at is not None and item.published_at <= cutoff
        ]
        if not eligible:
            raise ValueError("INSUFFICIENT_EVIDENCE: cannot draft a thesis without eligible public evidence")
        ids = list(dict.fromkeys(item.evidence_id for item in eligible))
        mock = _deterministic_mock_output(eligible)
        request = StructuredLLMRequest(
            purpose="ai_thesis_draft",
            response_model=ThesisDraftOutput,
            facts={"symbol": symbol, "as_of": cutoff.isoformat(), "evidence": _evidence_facts(eligible)},
            evidence_ids=ids,
            numeric_fact_tokens=sorted(_text_tokens(eligible)),
            grounded_responses=[mock.model_dump(mode="json")],
            generation_mode="select" if conservative else "generate",
            system_instruction=(
                "Create a concise investment-thesis draft from only the supplied evidence. "
                "Every material statement must cite one or more allowed evidence IDs. "
                "Do not count evidence items or introduce any numerical expression unless it is "
                "copied verbatim from supplied evidence text. Do not invent prices, forecasts, "
                "recommendations, or trade actions. Every statement must reuse at least one "
                "material term verbatim from the evidence it cites and must not add new "
                "content-bearing English terms. Write user-facing statements primarily in natural "
                "simplified Chinese, faithfully interpreting the cited evidence. Retain a cited "
                "company/product proper name or a short source term where needed for the verbatim "
                "grounding check; never copy an entire English sentence into the explanation. "
                "Do not introduce business claims beyond the cited source. "
                "Return only JSON matching the response schema."
            ),
        )
        result = await self.llm.complete_structured(request)
        if not isinstance(result, ThesisDraftOutput):
            raise LLMError("schema_validation", "LLM returned the wrong thesis result type")
        allowed = set(ids)
        if set(result.evidence_ids) - allowed or any(
            set(statement.evidence_ids) - allowed
            for group in (
                result.core_drivers,
                result.key_assumptions,
                result.execution_risks,
                result.invalidation_conditions,
            )
            for statement in group
        ):
            raise LLMError(
                "invalid_evidence_reference", "thesis draft cites evidence outside the supplied set"
            )
        if _output_tokens(result) - _text_tokens(eligible):
            raise LLMError(
                "unsupported_numeric_fact", "thesis draft introduced a numeric fact absent from evidence"
            )
        _validate_qualitative_support(result, eligible)
        payload = {
            "symbol": symbol,
            "as_of": cutoff.isoformat(),
            "draft": result.model_dump(mode="json"),
            "model": self.llm.model_name,
        }
        digest = _hash(payload)
        profile = ThesisProfile(
            **result.model_dump(),
            symbol=symbol,
            thesis_version_id="TV" + digest[7:23],
            as_of=cutoff,
            status=ThesisStatus.AI_DRAFT,
            generation=ThesisGenerationMetadata(model=self.llm.model_name),
        )
        item = EvidenceItem(
            evidence_id="E" + digest[7:23],
            evidence_type="thesis",
            provider="ai_thesis_generator",
            symbol=symbol,
            title="AI 投资逻辑草稿",
            source_record_id=profile.thesis_version_id,
            retrieved_at=utc(self.clock()),
            as_of=cutoff,
            content_hash=digest,
            structured_data=profile.model_dump(mode="json"),
        )
        if persist:
            self.store.add_thesis(profile, item)
        return profile, item
