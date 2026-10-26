"""ivs-sec-thesis-v1: which existing investment logic a filing forces you to re-check.

The filing lane answers what a company filed. This answers which part of the stored
thesis that touches, and what to check first. It selects statements that already exist
and maps them to SEC item codes. It writes nothing, scores nothing and calls no model.

Two point-in-time rules carry the whole module:

1. The thesis used is the latest version **strictly before** the filing's conservative
   eligibility boundary. Not `<=` (that admits a same-instant version), and not a
   one-second subtraction (that fails on subsecond timestamps).
2. A version must also have been *stored* before the boundary. Otherwise a thesis
   created today with a backdated `as_of` would silently become a historical input.

When either rule cannot be satisfied the answer is THESIS_NOT_AVAILABLE with a reason.
Missing is not HOLD, not "no impact", and not an invented investor premise.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .domain import EvidenceItem, FrozenModel, utc
from .sec import SEC_TIMEZONE, SEC_TIMEZONE_NAME, start_of_disclosed_day
from .sec_filings import FilingEvidence, FilingsProjection

SCHEMA_VERSION = "ivs-sec-thesis-v1"
THESIS_SCAN_LIMIT = 50

# Which groups of an existing thesis an SEC item most plausibly stresses. This is a
# deterministic review suggestion, never proof that the thesis changed.
ITEM_BUCKETS: dict[str, tuple[str, ...]] = {
    "EARNINGS": ("key_assumptions", "core_drivers"),
    "NEW_OBLIGATION": ("invalidation_conditions", "key_assumptions"),
    "ACCELERATED_OBLIGATION": ("invalidation_conditions",),
    "IMPAIRMENT": ("key_assumptions", "invalidation_conditions"),
    "ACQUISITION_DISPOSAL": ("core_drivers", "invalidation_conditions"),
    "AGREEMENT": ("core_drivers", "key_assumptions"),
    "AGREEMENT_TERMINATED": ("core_drivers", "invalidation_conditions"),
    "BANKRUPTCY": ("invalidation_conditions", "execution_risks"),
    "EXIT_COSTS": ("execution_risks", "key_assumptions"),
    "DELISTING": ("invalidation_conditions",),
    "AUDITOR_CHANGE": ("invalidation_conditions", "execution_risks"),
    "NON_RELIANCE": ("key_assumptions", "invalidation_conditions"),
    "CONTROL_CHANGE": ("core_drivers", "invalidation_conditions"),
    "LEADERSHIP": ("execution_risks", "core_drivers"),
    "REG_FD": ("key_assumptions",),
    "OTHER_EVENTS": ("key_assumptions",),
    # A periodic report has no item codes, but it is exactly where key assumptions
    # and invalidation conditions get tested. Ownership/registration forms are left
    # unmapped on purpose: they do not, by themselves, stress an operating assumption.
    "PERIODIC_REPORT": ("key_assumptions", "invalidation_conditions"),
    "FOREIGN_INTERIM": ("key_assumptions",),
    "PROXY": ("execution_risks",),
}

BUCKET_LABELS = {
    "core_drivers": "核心驱动",
    "key_assumptions": "关键假设",
    "execution_risks": "执行风险",
    "invalidation_conditions": "失效条件",
}

STATEMENTS_PER_BUCKET = 2
STATEMENTS_TOTAL = 4

NO_IMPACT_CLAIM = (
    "申报存在是事实；条目只提出应当核查什么。本结果不推断盈利影响、不推断股价因果，"
    "也不代表既有逻辑已经失效。"
)


class ThesisCutoff(FrozenModel):
    """The strict instant before which a thesis must already have existed."""

    boundary: datetime | None
    basis: Literal["ACCEPTED_AT", "START_OF_FILING_DATE", "UNKNOWN"]
    timezone: str | None
    meaning: str


class ThesisAvailability(FrozenModel):
    thesis_as_of: datetime | None
    stored_available_at: datetime | None
    eligible_before: datetime | None
    selection_basis: Literal[
        "STRICT_VERSION_SCAN", "PUBLIC_READER_ONLY", "NOT_APPLIED"]
    meaning: str


class StatementReview(FrozenModel):
    bucket: str
    bucket_label: str
    text: str
    evidence_ids: list[str]


class FilingClaims(FrozenModel):
    impact_asserted: bool = False
    price_effect_asserted: bool = False
    basis: str = NO_IMPACT_CLAIM


class FilingThesisDelta(FrozenModel):
    schema_version: Literal["ivs-sec-thesis-v1"] = SCHEMA_VERSION
    evidence_id: str
    symbol: str
    accession: str
    event_class: str
    event_label: str
    cutoff: ThesisCutoff
    status: Literal["THESIS_READY", "THESIS_UNCONFIRMED", "THESIS_NOT_AVAILABLE"]
    thesis_version_id: str | None = None
    thesis_status: str | None = None
    availability: ThesisAvailability
    statements: list[StatementReview] = Field(default_factory=list)
    review_questions: list[str] = Field(default_factory=list)
    next_check: str
    next_action: Literal["REVIEW_THESIS", "READ_FILING_FIRST"]
    reason: str | None = None
    claims: FilingClaims = Field(default_factory=FilingClaims)


class ThesisDeltaReport(FrozenModel):
    schema_version: Literal["ivs-sec-thesis-v1"] = SCHEMA_VERSION
    as_of: datetime
    deltas: list[FilingThesisDelta]
    by_status: dict[str, int]
    meaning: str = (
        "把申报映射到申报前已存在的投资逻辑。THESIS_NOT_AVAILABLE 是缺口，不是“无影响”；"
        "THESIS_UNCONFIRMED 表示草稿而非用户确认的判断。"
    )
    limitations: list[str] = Field(default_factory=list)


def cutoff_for(filing: FilingEvidence) -> ThesisCutoff:
    """Conservative earliest instant at which this filing could have been known.

    Second precision uses the acceptance instant. A date-only disclosure uses the
    start of that declared Eastern day, so a thesis written later the same day is
    excluded rather than admitted as prior knowledge.
    """
    if filing.time_precision == "SECOND" and filing.accepted_at is not None:
        return ThesisCutoff(boundary=utc(filing.accepted_at), basis="ACCEPTED_AT",
                            timezone="UTC",
                            meaning="SEC 接收时间（秒级）是该申报可知的最早时刻。")
    if filing.time_precision == "DATE" and filing.filing_date is not None:
        boundary = start_of_disclosed_day(filing.filing_date, timezone=SEC_TIMEZONE)
        return ThesisCutoff(boundary=boundary, basis="START_OF_FILING_DATE",
                            timezone=SEC_TIMEZONE_NAME,
                            meaning=("仅公布日期时，取该日（美东）开始作为保守上界："
                                     "当日或事后写入的逻辑都不算事前已知。"))
    return ThesisCutoff(boundary=None, basis="UNKNOWN", timezone=None,
                        meaning="申报时间精度未知，无法确定事前边界。")


def _strict_prior(store, symbol: str, boundary: datetime, *, limit: int = THESIS_SCAN_LIMIT):
    """Latest stored version strictly before the boundary, availability included.

    Returns `(profile, evidence, selection, rejected)`. `rejected` is the closest-dated
    version (the scan runs newest `as_of` first) that carried a pre-filing `as_of` but was
    *recorded* after the boundary. It is never used as prior knowledge; it is returned so
    the caller can say why the gap exists instead of reporting the same "no version"
    wording that a symbol with no history gets.
    """
    from .thesis import ThesisProfile

    connect = getattr(store, "_connect", None)
    if callable(connect):
        with connect() as db:
            rows = db.execute(
                "SELECT payload, evidence_payload FROM thesis_versions "
                "WHERE symbol=? AND as_of < ? ORDER BY as_of DESC, rowid DESC LIMIT ?",
                (symbol, utc(boundary).isoformat(), limit)).fetchall()
        rejected = None
        for payload, evidence_payload in rows:
            profile = ThesisProfile.model_validate_json(payload)
            evidence = EvidenceItem.model_validate_json(evidence_payload)
            # The SQL filter is strict, but the parsed values are the authority: a
            # stored string that sorts oddly must not become an eligible version.
            if profile.as_of >= boundary:
                continue
            if evidence.retrieved_at >= boundary:
                # Recorded after the disclosure. A backdated thesis is not prior
                # knowledge, but its existence is the honest explanation for the gap.
                if rejected is None:
                    rejected = evidence
                continue
            return profile, evidence, "STRICT_VERSION_SCAN", None
        return None, None, "STRICT_VERSION_SCAN", rejected

    # Duck-typed store (tests, alternate implementations): the public reader is
    # inclusive, so equality is detected and reported instead of resolved by guessing.
    profile = store.get_thesis(symbol, boundary)
    if profile is None:
        return None, None, "PUBLIC_READER_ONLY", None
    if profile.as_of >= boundary:
        return None, None, "PUBLIC_READER_ONLY", None
    evidence = None
    reader = getattr(store, "get_thesis_evidence", None)
    if callable(reader):
        evidence = reader(profile.thesis_version_id)
    if evidence is not None and evidence.retrieved_at >= boundary:
        return None, None, "PUBLIC_READER_ONLY", evidence
    return profile, evidence, "PUBLIC_READER_ONLY", None


def _statements(profile, event_class: str) -> list[StatementReview]:
    output: list[StatementReview] = []
    for bucket in ITEM_BUCKETS.get(event_class, ()):
        for statement in list(getattr(profile, bucket, ()))[:STATEMENTS_PER_BUCKET]:
            if len(output) >= STATEMENTS_TOTAL:
                return output
            output.append(StatementReview(
                bucket=bucket, bucket_label=BUCKET_LABELS.get(bucket, bucket),
                text=statement.text, evidence_ids=list(statement.evidence_ids)))
    return output


def _next_check(filing: FilingEvidence, statements: list[StatementReview]) -> str:
    if filing.review_questions:
        return filing.review_questions[0]
    if statements:
        return f"复核{statements[0].bucket_label}是否仍然成立：{statements[0].text}"
    return "先读原始申报，再判断它是否触及任何既有假设。"


def project_one(filing: FilingEvidence, store) -> FilingThesisDelta:
    """One filing against the thesis that existed before it."""
    cutoff = cutoff_for(filing)
    base = dict(evidence_id=filing.evidence_id, symbol=filing.symbol,
                accession=filing.accession, event_class=filing.event_class,
                event_label=filing.event_label, cutoff=cutoff,
                review_questions=list(filing.review_questions))
    if cutoff.boundary is None:
        return FilingThesisDelta(
            **base, status="THESIS_NOT_AVAILABLE",
            availability=ThesisAvailability(thesis_as_of=None, stored_available_at=None,
                                            eligible_before=None, selection_basis="NOT_APPLIED",
                                            meaning="没有可用于比较的事前边界。"),
            next_check="先确认申报的接收时间，再谈它是否影响既有逻辑。",
            next_action="READ_FILING_FIRST", reason="TIME_UNKNOWN")

    profile, evidence, selection, rejected = _strict_prior(store, filing.symbol,
                                                           cutoff.boundary)
    if profile is None:
        stored = None
        if rejected is not None:
            # A version exists and was excluded on purpose. Saying "no version" here
            # would be a false statement about stored history.
            reason = "RECORDED_AFTER_DISCLOSURE"
            stored = rejected.retrieved_at
            detail = ("存在日期早于本次申报的投资逻辑版本，但它在披露之后才被保存，"
                      "不能作为事前已知；这与“从未有过版本”不同。")
        elif selection == "STRICT_VERSION_SCAN":
            reason = "NO_PRIOR_VERSION"
            detail = "该标的在申报边界之前没有已保存的投资逻辑版本。"
        else:
            reason = "PRIOR_VERSION_UNRESOLVED"
            detail = "只读查询无法证明边界之前是否存在更早版本；不猜测。"
        return FilingThesisDelta(
            **base, status="THESIS_NOT_AVAILABLE",
            availability=ThesisAvailability(
                thesis_as_of=None, stored_available_at=stored,
                eligible_before=cutoff.boundary, selection_basis=selection,
                meaning=detail),
            next_check=_next_check(filing, []),
            next_action="READ_FILING_FIRST", reason=reason)

    statements = _statements(profile, filing.event_class)
    status = "THESIS_READY" if profile.status.value == "USER_CONFIRMED" else "THESIS_UNCONFIRMED"
    return FilingThesisDelta(
        **base, status=status, thesis_version_id=profile.thesis_version_id,
        thesis_status=profile.status.value,
        availability=ThesisAvailability(
            thesis_as_of=profile.as_of,
            stored_available_at=evidence.retrieved_at if evidence is not None else None,
            eligible_before=cutoff.boundary, selection_basis=selection,
            meaning=("使用申报边界之前已存在且已保存的最近版本；"
                     "事后或同一时刻写入的版本不会被采用。")),
        statements=statements,
        next_check=_next_check(filing, statements),
        next_action="REVIEW_THESIS" if statements else "READ_FILING_FIRST",
    )


def project(projection: FilingsProjection, store) -> ThesisDeltaReport:
    """Every filing in a projection, against its own point-in-time thesis."""
    deltas = [project_one(filing, store) for filing in projection.filings]
    counts: dict[str, int] = {}
    for delta in deltas:
        counts[delta.status] = counts.get(delta.status, 0) + 1
    limitations = [
        "只读取已保存的 Thesis 版本，不生成、不修改、不迁移。",
        "THESIS_NOT_AVAILABLE 表示缺口：没有事前版本或时间未知，不代表没有影响。",
        "条目到条件分组的映射是确定性复核建议，不构成盈利影响或股价因果判断。",
    ]
    if any(delta.status == "THESIS_UNCONFIRMED" for delta in deltas):
        limitations.append("THESIS_UNCONFIRMED 是 AI 草稿，不是用户确认的投资判断。")
    return ThesisDeltaReport(as_of=projection.as_of, deltas=deltas, by_status=counts,
                             limitations=limitations)
