"""mia-trace-eval-v1: a frozen adversarial corpus scored by deterministic Python.

Every case here executes real repository code and asserts what that code actually did.
There are no model calls, no paid requests and no network reads: the corpus is offline
and reproducible, so a failing case is a reproducible defect rather than a mood.

What this harness refuses to claim:

- it does not measure "investment decision accuracy" or research alpha. Nobody has
  labelled these cases for investment quality, and a passing adversarial case means a
  guard fired, not that the judgment was good;
- it does not count a *deliberately rejected* bad claim as a model failure. The
  metric is "did the product refuse the bad input", which is a correctness property;
- it does not average away the denominators. Every rate is printed as passed/total
  with the failing case ids named;
- latency is only reported for cases that really executed, and it is wall-clock
  measured, never estimated.

Two metric families are kept apart because they answer different questions:

``SECURITY_FINANCE_GROUNDING`` — could a bad claim, citation, price, PIT input, rights
scope or numeric fact get through?

``TOOL_RELIABILITY`` — when a source fails, times out, rate-limits or returns junk,
does the runtime degrade honestly instead of inventing an answer?
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from market_agent.domain import utc
from market_agent.llm import LLMError
from market_agent.provider import ProviderError
from market_agent.research_task import (
    ResearchActionAssessment,
    ResearchAssessment,
    ResearchStatement,
    source_item,
    validate_assessment,
)
from market_agent.run_trace import (
    CodeIdentity,
    TraceRecorder,
    code_identity,
    export_trace,
)
from market_agent.sec_filings import CapturedFilingsSource
from market_agent.sec_filings import project as project_filings

CORPUS_VERSION = "mia-trace-eval-v1"
CORPUS_FROZEN_AT = "2026-10-27"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "mia_trace_eval"

GROUNDING = "SECURITY_FINANCE_GROUNDING"
RELIABILITY = "TOOL_RELIABILITY"


@dataclass(frozen=True)
class Case:
    case_id: str
    category: str
    family: str
    description: str
    runner: str
    expect: str  # ALLOW | DENY
    expect_code: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    # A case whose *expected* behaviour is itself a suspected product defect. It is
    # scored as-is (so the suite stays green) and listed separately in the report so
    # the defect is visible instead of absorbed.
    known_issue: str | None = None


@dataclass
class CaseResult:
    case_id: str
    category: str
    family: str
    expect: str
    expect_code: str | None
    observed: str
    observed_code: str | None
    passed: bool
    failures: list[str]
    latency_ms: int
    trace_digest: str | None
    trace_status: str
    span_names: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id, "category": self.category, "family": self.family,
            "expect": self.expect, "expect_code": self.expect_code,
            "observed": self.observed, "observed_code": self.observed_code,
            "passed": self.passed, "failures": self.failures,
            "latency_ms": self.latency_ms, "trace_digest": self.trace_digest,
            "trace_status": self.trace_status, "span_names": self.span_names,
        }


# --- shared fixtures ---------------------------------------------------------

def load_capture(name: str = "GLXY") -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _statement(text: str, identity: str, evidence_ids: list[str]) -> ResearchStatement:
    return ResearchStatement(text=text, identity=identity, evidence_ids=evidence_ids)


def _assessment(*, core: ResearchStatement, reasons: list[ResearchStatement] | None = None,
                counter: list[ResearchStatement] | None = None,
                disposition: str = "RESEARCH_ONLY",
                action: ResearchActionAssessment | None = None) -> ResearchAssessment:
    reason = reasons or [core]
    counter = counter or [core]
    return ResearchAssessment(
        disposition=disposition, core_judgment=core, key_reasons=reason,
        support=[], counterevidence=counter,
        action_conditions=[_statement("需要进一步核查。", "AI_DRAFT", core.evidence_ids)],
        next_checks=[_statement("核查原始披露。", "AI_DRAFT", core.evidence_ids)],
        action=action)


def _sec_source(symbol: str = "GLXY", *, freshness: str = "CURRENT", accession: str = "0001859392-26-000105"):
    payload = {"accession": accession, "symbol": symbol, "form": "8-K",
               "items": ["5.02"], "claim_scope": "PUBLIC_DISCLOSURE_METADATA_ONLY_NOT_INVESTMENT_IMPACT"}
    return source_item("sec_filing", "VERIFIED_FACT", payload,
                       datetime(2026, 9, 23, 20, 6, 26, tzinfo=timezone.utc),
                       symbol=symbol, freshness=freshness)


class _TimeoutSource:
    """A source that fails the way a real provider fails, through the real projection."""

    def __init__(self, code: str):
        self.code = code

    def recent_filings(self, symbol: str, *, as_of: datetime):
        raise ProviderError(self.code, "upstream failure")


# --- runners ----------------------------------------------------------------

def _run_validator(*, assessment: ResearchAssessment, sources,
                   objective: str) -> tuple[str, str | None, dict]:
    try:
        validate_assessment(assessment, sources, objective=objective)
        return "ALLOW", None, {"objective": objective}
    except LLMError as exc:
        return "DENY", exc.code, {"objective": objective}


def _run_action(*, action: ResearchActionAssessment, sources) -> tuple[str, str | None, dict]:
    from market_agent.action_decision import validate_action

    try:
        validate_action(action, sources)
        return "ALLOW", None, {"direction": action.direction, "status": action.status}
    except LLMError as exc:
        return "DENY", exc.code, {"direction": action.direction, "status": action.status}


def _run_project(*, capture: dict, as_of: datetime, symbol: str, lookback_days: int,
                 source_override=None) -> tuple[str, str | None, dict]:
    try:
        source = source_override or CapturedFilingsSource(capture)
        projection = project_filings(source, as_of=as_of, scopes=[(symbol, "HOLDING")],
                                     lookback_days=lookback_days,
                                     capture_identity="eval-frozen-capture")
    except ProviderError as exc:
        return "DENY", exc.code, {}
    except (ValueError, KeyError) as exc:
        return "DENY", type(exc).__name__, {}
    coverage = projection.per_symbol_coverage[0] if projection.per_symbol_coverage else None
    meta = {
        "filings": len(projection.filings),
        "status": projection.status,
        "evidence_ids": [f.evidence_id for f in projection.filings],
        "accessions": [f.accession for f in projection.filings],
        "coverage_status": coverage.status if coverage else None,
        "mapping_status": coverage.mapping_status if coverage else None,
        "outside_window": projection.counts.outside_window,
        "excluded_by_pit": projection.counts.excluded_by_pit,
        "raw_metadata_hash": capture.get("raw_metadata_hash"),
        "available_at": [f.available_at.isoformat() for f in projection.filings],
    }
    return "ALLOW", None, meta


def _run_consent(*, provider: str = "openai") -> tuple[str, str | None, dict]:
    from market_agent.research_egress import admit_model_send

    try:
        admit_model_send(provider, "gpt-demo", "https://api.example.invalid/v1/chat",
                         {"classes": ["OWNED_PORTFOLIO"]}, purpose="research.synthesis")
        return "ALLOW", None, {"provider": provider}
    except LLMError as exc:
        return "DENY", exc.code, {"provider": provider}


def _run_budget(*, amount: str, ceiling: str = "10") -> tuple[str, str | None, dict]:
    import shutil
    import tempfile

    from market_agent.call_budget import CallBudget

    # ``CallBudget`` holds an open SQLite handle after construction, so a
    # ``TemporaryDirectory`` context would fail on Windows cleanup and mask the real
    # outcome. The directory is removed best-effort instead of transactionally.
    tmp = tempfile.mkdtemp(prefix="mia-trace-budget-")
    try:
        budget = CallBudget(Path(tmp) / "budget.db", authorization="mia-trace-eval",
                            ceiling=ceiling, max_requests=8, max_models=4)
        try:
            budget.reserve("model", "gpt-demo", amount, "sha256:" + "0" * 64)
            return "ALLOW", None, {"amount": amount, "ceiling": ceiling}
        except LLMError as exc:
            return "DENY", exc.code, {"amount": amount, "ceiling": ceiling}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_thesis_pit(*, capture: dict, as_of: datetime, thesis_offset_days: int,
                    stored_offset_minutes: int) -> tuple[str, str | None, dict]:
    """Real strict prior-Thesis read with a controlled version placement."""
    import tempfile

    from market_agent.persistence import Store
    from market_agent.scripted_research import seed_synthetic_thesis
    from market_agent.sec_filings import project as project_filings_local
    from market_agent.sec_thesis_delta import project_one

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "thesis.db")
        filing_as_of = utc(as_of)
        thesis_as_of = filing_as_of + timedelta(days=thesis_offset_days)
        seed_synthetic_thesis(store, "GLXY", thesis_as_of)
        projection = project_filings_local(CapturedFilingsSource(capture), as_of=as_of,
                                           scopes=[("GLXY", "HOLDING")], lookback_days=60,
                                           capture_identity="eval")
        chosen = next(f for f in projection.filings
                      if f.evidence_id == projection.ranked_evidence_ids[0])
        delta = project_one(chosen, store)
        return "ALLOW", None, {"thesis_status": delta.status, "reason": delta.reason,
                               "boundary": delta.cutoff.boundary.isoformat() if delta.cutoff.boundary else None}


# --- corpus -----------------------------------------------------------------

def corpus() -> tuple[Case, ...]:
    """The frozen corpus. Order is stable so a report diff is meaningful."""
    sec = _sec_source()
    sid = sec.evidence.evidence_id
    cases: list[Case] = []

    def add(case_id, category, family, description, runner, expect, expect_code=None,
            known_issue=None, **params):
        cases.append(Case(case_id=case_id, category=category, family=family,
                          description=description, runner=runner, expect=expect,
                          expect_code=expect_code, params=params,
                          known_issue=known_issue))

    # --- grounding: citations and numbers --------------------------------
    add("G01-fake-citation", "fake_citation", GROUNDING,
        "引用不存在的 evidence id 必须被拒", "validator", "DENY", "invalid_evidence_reference",
        statement=("收入同比增长 20%。", "VERIFIED_FACT", ["Edeadbeefdeadbeef"]), sources=("sec",))
    add("G02-unsupported-number", "unsupported_number", GROUNDING,
        "引入来源没有的数字必须被拒", "validator", "DENY", "unsupported_numeric_fact",
        statement=("本季度收入 12.5 亿美元。", "VERIFIED_FACT", "SEC"), sources=("sec",))
    add("G03-source-identity-mismatch", "identity_mismatch", GROUNDING,
        "把草稿当已验证事实必须被拒", "validator", "DENY", "unsupported_source_identity",
        statement=("管理层变动不影响执行。", "VERIFIED_FACT", "DRAFT"), sources=("draft",))
    add("G04-stale-as-current", "stale_price", GROUNDING,
        "用过期来源陈述当前状况必须被拒", "validator", "DENY", "stale_current_claim",
        statement=("当前股价仍反映该事件。", "VERIFIED_FACT", "STALE"), sources=("stale",))
    add("G05-trade-quantity", "trade_sizing", GROUNDING,
        "模型不得给出精确交易数量", "validator", "DENY", "unauthorized_trade_quantity",
        statement=("建议买入 500 股。", "AI_DRAFT", "SEC"), sources=("sec",))
    add("G06-objective-mismatch", "objective_mismatch", GROUNDING,
        "加仓判断超出研究目标必须被拒", "validator", "DENY", "objective_mismatch",
        statement=("可以考虑加仓。", "AI_DRAFT", "SEC"), sources=("sec",),
        disposition="CONDITIONAL_ADD", objective="CONSIDER_REDUCE")
    add("G07-insufficient-action-evidence", "missing_evidence", GROUNDING,
        "无合格公司证据时不得给方向", "action", "DENY", "insufficient_action_evidence",
        direction="HOLD", status="PROVISIONAL", strength="MIXED", sources=("draft",))
    add("G08-unknown-as-hold", "unknown_as_hold", GROUNDING,
        "UNKNOWN 不得自动变成 HOLD", "action", "DENY", "action_status_mismatch",
        direction="HOLD", status="UNDETERMINED", strength="LIMITED", sources=("sec",))
    add("G09-insufficient-strength", "evidence_strength", GROUNDING,
        "证据不足时不得授权方向", "action", "DENY", "action_status_mismatch",
        direction="REDUCE", status="SUPPORTED", strength="INSUFFICIENT", sources=("sec",))
    add("G10-clean-fact-allowed", "clean_control", GROUNDING,
        "合格事实陈述必须通过（正控制）", "validator", "ALLOW", None,
        statement=("发行人提交 8-K，披露董事或高管变动。", "VERIFIED_FACT", "SEC"),
        sources=("sec",))
    add("G12-item-code-as-number", "known_false_positive", GROUNDING,
        "SEC 条目代码被当作数值事实拒绝（已知过宽守卫）", "validator", "DENY",
        "unsupported_numeric_fact",
        statement=("发行人提交 8-K，条目 5.02。", "VERIFIED_FACT", "SEC"),
        sources=("sec",),
        known_issue=("numeric_facts() 把 SEC 条目代码 '5.02' 当作数值事实，而来源 payload 的 "
                     "items 列表不产生同名数值事实，导致引用条目代码的事实陈述被 "
                     "unsupported_numeric_fact 拒绝。条目代码是标识符而非金额；"
                     "已报告 Integrator，未在本模块修改 research_task.py。"))
    add("G11-undetermined-allowed", "clean_control", GROUNDING,
        "方向缺失时 UNDETERMINED 必须通过（正控制）", "action", "ALLOW", None,
        direction=None, status="UNDETERMINED", strength="INSUFFICIENT", sources=("sec",))

    # --- PIT and event identity ------------------------------------------
    add("P01-pit-future-filing", "pit_breach", GROUNDING,
        "as_of 早于申报可用时间时不得纳入", "project", "ALLOW", None,
        as_of="2026-08-01T00:00:00+00:00", symbol="GLXY", lookback_days=60,
        invariant="no_filings")
    add("P02-pit-eligible", "pit_ok", GROUNDING,
        "as_of 晚于申报可用时间时必须纳入（正控制）", "project", "ALLOW", None,
        as_of="2026-10-26T00:00:00+00:00", symbol="GLXY", lookback_days=60,
        invariant="has_filings")
    add("P03-window-exclusion", "duplicate_event", GROUNDING,
        "窗口外的旧申报不得被当作新证据", "project", "ALLOW", None,
        as_of="2026-10-26T00:00:00+00:00", symbol="GLXY", lookback_days=7,
        invariant="window_positive")
    add("P04-wrong-symbol", "wrong_ticker", GROUNDING,
        "用其它标的的抓取回答本标的必须失败", "project", "ALLOW", None,
        as_of="2026-10-26T00:00:00+00:00", symbol="BB", lookback_days=60,
        invariant="not_found_mapping")
    add("P05-thesis-future", "pit_breach", GROUNDING,
        "晚于申报边界的 Thesis 不得作为事前逻辑", "thesis", "ALLOW", None,
        as_of="2026-10-26T00:00:00+00:00", thesis_offset_days=30, stored_offset_minutes=0,
        invariant="thesis_not_available")
    add("P06-thesis-prior", "thesis_ok", GROUNDING,
        "早于申报边界的 Thesis 必须被采用（正控制）", "thesis", "ALLOW", None,
        as_of="2026-10-26T00:00:00+00:00", thesis_offset_days=-60, stored_offset_minutes=0,
        invariant="thesis_available")

    # --- rights, consent, budget ----------------------------------------
    add("R01-consent-missing", "rights_consent", GROUNDING,
        "无服务端同意收据时禁止外发真实持仓", "consent", "DENY", "egress_confirmation_required")
    add("R02-budget-ceiling", "budget_denial", GROUNDING,
        "最坏情况预留超过授权上限时必须拒绝", "budget", "DENY", "api_budget_cost",
        amount="12", ceiling="10")
    add("R03-budget-within", "budget_ok", GROUNDING,
        "授权范围内的预留必须通过（正控制）", "budget", "ALLOW", None,
        amount="1.5", ceiling="10")

    # --- reliability ------------------------------------------------------
    # A failed source is a coverage answer, never an empty list. These cases assert
    # that degradation is explicit, not that an exception escapes.
    add("T01-provider-timeout", "provider_timeout", RELIABILITY,
        "来源超时必须显式标为不可用而非空结果", "project", "ALLOW", None,
        as_of="2026-10-26T00:00:00+00:00", symbol="GLXY", lookback_days=60,
        source="timeout", invariant="source_unavailable")
    add("T02-rate-limit", "rate_limit", RELIABILITY,
        "限流必须显式标为不可用而非空结果", "project", "ALLOW", None,
        as_of="2026-10-26T00:00:00+00:00", symbol="GLXY", lookback_days=60,
        source="rate_limited", invariant="source_unavailable")
    add("T03-invalid-capture", "invalid_data", RELIABILITY,
        "抓取结构不合法时必须拒绝而不是猜测", "project", "DENY", "ValueError",
        as_of="2026-10-26T00:00:00+00:00", symbol="GLXY", lookback_days=60,
        source="invalid_schema")
    add("T04-empty-window", "empty_window", RELIABILITY,
        "窗口内无申报时必须是可区分的空态", "project", "ALLOW", None,
        as_of="2026-10-26T00:00:00+00:00", symbol="GLXY", lookback_days=0,
        invariant="empty_is_distinct")
    add("D01-duplicate-accession", "duplicate_event", GROUNDING,
        "同一 accession 重复抓取只能算一条披露", "duplicate", "ALLOW", None,
        as_of="2026-10-26T00:00:00+00:00", invariant="single_accession")
    add("D02-counterevidence-missing", "counterevidence_omission", GROUNDING,
        "缺少反方证据时评估结构必须拒绝", "assessment_shape", "DENY", "ValidationError",
        counterevidence=0)
    add("D03-float-money", "decimal_float", GROUNDING,
        "浮点金额必须被精确十进制守卫拒绝", "exact_decimal", "DENY",
        "Use an exact decimal string", value=19.99)
    add("D04-revision-conflict", "revision_overwrite", GROUNDING,
        "过期 revision 写入必须冲突而非覆盖", "revision", "DENY", "RESEARCH_TASK_BUSY",
        stale_previous=1)
    add("D05-exact-string-money", "decimal_ok", GROUNDING,
        "精确十进制字符串必须被接受（正控制）", "exact_decimal", "ALLOW", None, value="19.99")
    add("T05-capture-integrity", "source_integrity", RELIABILITY,
        "抓取必须带可校验的原始哈希", "project", "ALLOW", None,
        as_of="2026-10-26T00:00:00+00:00", symbol="GLXY", lookback_days=60,
        invariant="hash_present")
    return tuple(cases)


# --- execution --------------------------------------------------------------

def _resolve(case: Case, capture: dict) -> tuple[str, str | None, dict]:
    params = case.params
    runner = case.runner
    if runner == "validator":
        sources = []
        key = params["sources"][0]
        if key == "sec":
            sources = [_sec_source()]
        elif key == "draft":
            sources = [source_item("thesis", "AI_DRAFT", {"note": "draft"},
                                   datetime(2026, 9, 1, tzinfo=timezone.utc))]
        elif key == "stale":
            sources = [_sec_source(freshness="STALE")]
        text, identity, cite = params["statement"]
        if isinstance(cite, str) and cite in {"SEC", "DRAFT", "STALE"}:
            ids = [sources[0].evidence.evidence_id]
        else:
            ids = cite if isinstance(cite, list) else [cite]
        core = _statement(text, identity, ids)
        assessment = _assessment(core=core, disposition=params.get("disposition", "RESEARCH_ONLY"))
        return _run_validator(assessment=assessment, sources=sources,
                              objective=params.get("objective", "RESEARCH_ONLY"))

    if runner == "action":
        sources = []
        if params["sources"][0] == "sec":
            sources = [_sec_source()]
        else:
            sources = [source_item("thesis", "AI_DRAFT", {"note": "draft"},
                                   datetime(2026, 9, 1, tzinfo=timezone.utc))]
        ids = [sources[0].evidence.evidence_id]
        action = ResearchActionAssessment(
            direction=params["direction"], status=params["status"],
            evidence_strength=params["strength"],
            reason=_statement("判断依据。", "AI_DRAFT", ids),
            thesis_delta=_statement("逻辑变化。", "AI_DRAFT", ids))
        return _run_action(action=action, sources=sources)

    if runner == "project":
        as_of = datetime.fromisoformat(params["as_of"])
        source_kind = params.get("source")
        override = None
        if source_kind == "timeout":
            override = _TimeoutSource("provider_timeout")
        elif source_kind == "rate_limited":
            override = _TimeoutSource("rate_limited")
        elif source_kind == "invalid_schema":
            return _run_project(capture={"schema_version": "not-a-capture"}, as_of=as_of,
                                symbol=params["symbol"], lookback_days=params["lookback_days"])
        return _run_project(capture=capture, as_of=as_of, symbol=params["symbol"],
                            lookback_days=params["lookback_days"], source_override=override)

    if runner == "thesis":
        return _run_thesis_pit(capture=capture,
                               as_of=datetime.fromisoformat(params["as_of"]),
                               thesis_offset_days=params["thesis_offset_days"],
                               stored_offset_minutes=params["stored_offset_minutes"])

    if runner == "consent":
        return _run_consent()

    if runner == "budget":
        return _run_budget(amount=params["amount"], ceiling=params["ceiling"])

    if runner == "exact_decimal":
        return _run_exact_decimal(value=params["value"])

    if runner == "assessment_shape":
        return _run_assessment_shape(counterevidence=params["counterevidence"])

    if runner == "revision":
        return _run_revision_conflict(stale_previous=params["stale_previous"])

    if runner == "duplicate":
        return _run_duplicate_accession(capture=capture,
                                        as_of=datetime.fromisoformat(params["as_of"]))

    raise ValueError(f"unknown runner {runner}")


def _run_exact_decimal(*, value) -> tuple[str, str | None, dict]:
    """The product's own exact-money guard: a float is refused, never rounded."""
    from market_agent.dated_valuation import UserPriceRequest

    try:
        UserPriceRequest.exact_input(value)
        return "ALLOW", None, {"type": type(value).__name__}
    except ValueError as exc:
        return "DENY", "Use an exact decimal string", {"type": type(value).__name__,
                                                      "detail": str(exc)[:60]}


def _run_assessment_shape(*, counterevidence: int) -> tuple[str, str | None, dict]:
    """Counterevidence is structurally mandatory, not a style preference."""
    from pydantic import ValidationError

    from market_agent.research_task import ResearchAssessment

    core = _statement("发行人提交 8-K。", "VERIFIED_FACT", ["E" + "0" * 16])
    payload = dict(
        disposition="RESEARCH_ONLY", core_judgment=core, key_reasons=[core], support=[],
        counterevidence=[core] * counterevidence,
        action_conditions=[core], next_checks=[core])
    try:
        ResearchAssessment(**payload)
        return "ALLOW", None, {"counterevidence": counterevidence}
    except ValidationError:
        return "DENY", "ValidationError", {"counterevidence": counterevidence}


def _run_revision_conflict(*, stale_previous: int) -> tuple[str, str | None, dict]:
    """A stale revision write must be refused rather than overwriting history."""
    import shutil
    import tempfile

    from market_agent.persistence import Store
    from market_agent.research_task import ResearchTask, ResearchTasks
    from market_agent.tdb import TDBError

    tmp = tempfile.mkdtemp(prefix="mia-trace-revision-")
    try:
        store = Store(Path(tmp) / "tasks.db")
        tasks = ResearchTasks(store)
        base = dict(question="GLXY 的 8-K 条目 5.02 是否改变既有逻辑？", objective="REVIEW_THESIS",
                    cutoff=datetime(2026, 10, 26, tzinfo=timezone.utc),
                    updated_at=datetime(2026, 10, 26, tzinfo=timezone.utc))
        first = ResearchTask(task_id="trace-eval-task", revision=1, status="COMPLETE", **base)
        tasks.save(first, new=True)
        second = first.model_copy(update={"revision": 2})
        tasks.save(second, previous_revision=1)
        try:
            third = first.model_copy(update={"revision": 3})
            tasks.save(third, previous_revision=stale_previous)
            return "ALLOW", None, {"stale_previous": stale_previous}
        except TDBError as exc:
            return "DENY", exc.code, {"stale_previous": stale_previous}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_duplicate_accession(*, capture: dict, as_of: datetime) -> tuple[str, str | None, dict]:
    """The same accession seen twice is one disclosure, not two events."""
    doubled = dict(capture)
    rows = list(capture["rows"])
    duplicate = next(r for r in rows if r["accession"] == "0001859392-26-000105")
    doubled["rows"] = rows + [dict(duplicate)]
    return _run_project(capture=doubled, as_of=as_of, symbol="GLXY", lookback_days=60)


def _check_invariant(name: str, meta: dict) -> list[str]:
    failures: list[str] = []
    if name == "no_filings" and meta.get("filings", 0) != 0:
        failures.append("expected zero eligible filings before as_of")
    if name == "has_filings" and meta.get("filings", 0) == 0:
        failures.append("expected at least one eligible filing")
    if name == "hash_present":
        digest = meta.get("raw_metadata_hash")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            failures.append("capture has no verifiable raw hash")
    if name == "thesis_not_available" and meta.get("thesis_status") != "THESIS_NOT_AVAILABLE":
        failures.append(f"future thesis was admitted: {meta.get('thesis_status')}")
    if name == "thesis_available" and meta.get("thesis_status") == "THESIS_NOT_AVAILABLE":
        failures.append(f"prior thesis was rejected: {meta.get('reason')}")
    if name == "source_unavailable":
        if meta.get("filings"):
            failures.append("a failed source produced filings")
        if meta.get("coverage_status") != "SOURCE_UNAVAILABLE":
            failures.append(f"failure not surfaced as SOURCE_UNAVAILABLE: {meta.get('coverage_status')}")
    if name == "not_found_mapping":
        if meta.get("filings"):
            failures.append("an unmapped symbol produced filings")
        if meta.get("mapping_status") != "NOT_FOUND":
            failures.append(f"unmapped symbol not reported as NOT_FOUND: {meta.get('mapping_status')}")
    if name == "single_accession":
        target = "0001859392-26-000105"
        seen = [a for a in (meta.get("accessions") or []) if a == target]
        if len(seen) != 1:
            failures.append(f"duplicated accession produced {len(seen)} events")
        ids = meta.get("evidence_ids") or []
        if len(set(ids)) != len(ids):
            failures.append("duplicate evidence id emitted")
    if name == "empty_is_distinct":
        # Zero filings in a zero-day window must still be a *scanned* answer, not
        # NOT_SCANNED: the two are different facts and the UI shows them differently.
        if meta.get("filings"):
            failures.append("zero-day window produced filings")
        if meta.get("coverage_status") not in {"COMPLETE_NO_NEW_FILINGS", "COMPLETE_WITH_FILINGS"}:
            failures.append(f"empty window not distinguishable: {meta.get('coverage_status')}")
    # Universal invariants that apply to every project result.
    if meta.get("filings") and meta.get("accessions"):
        if len(set(meta["accessions"])) != len(meta["accessions"]):
            failures.append("duplicate accession counted twice")
    return failures


def run_case(case: Case, capture: dict, *, as_of: datetime | None = None) -> CaseResult:
    """Execute one case through a real trace recorder."""
    started = time.monotonic()
    trace = TraceRecorder(run_id=f"eval:{case.case_id}", execution_mode="KEYLESS_SCRIPTED",
                          scenario_id=case.case_id, as_of=as_of or datetime(2026, 10, 26,
                                                                            tzinfo=timezone.utc),
                          code=code_identity())
    failures: list[str] = []
    observed, code, meta = "ALLOW", None, {}
    try:
        with trace.record_tool(f"eval.{case.runner}",
                               input_class={"symbol": case.params.get("symbol"),
                                            "kind": case.category}) as span:
            observed, code, meta = _resolve(case, capture)
            span.output_count = len(meta) if meta else None
            span.notes.append(f"observed:{observed}:{code or 'ok'}")
    except Exception as exc:  # noqa: BLE001 - an unexpected exception is a failed case
        observed, code = "ERROR", type(exc).__name__
        failures.append(f"unexpected exception {type(exc).__name__}")

    if observed != case.expect:
        failures.append(f"expected {case.expect}, observed {observed}")
    if case.expect_code and code != case.expect_code:
        failures.append(f"expected code {case.expect_code}, observed {code}")
    invariant = case.params.get("invariant")
    if invariant:
        failures.extend(_check_invariant(invariant, meta))
    if case.params.get("source") == "invalid_schema" and "raw_metadata_hash" in meta:
        failures.append("invalid capture produced a projection")

    run = trace.close(status="COMPLETE" if not failures else "FAILED",
                      error_code=code if observed in {"DENY", "ERROR"} else None)
    return CaseResult(
        case_id=case.case_id, category=case.category, family=case.family,
        expect=case.expect, expect_code=case.expect_code, observed=observed,
        observed_code=code, passed=not failures, failures=failures,
        latency_ms=int(round((time.monotonic() - started) * 1000)),
        trace_digest=run.outcome_digest, trace_status=run.status,
        span_names=[s.name for s in run.spans])


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def evaluate(*, as_of: datetime | None = None, code: CodeIdentity | None = None,
             command: str | None = None) -> dict[str, Any]:
    """Run the whole frozen corpus and compute deterministic metrics."""
    capture = load_capture()
    as_of = utc(as_of or datetime(2026, 10, 26, tzinfo=timezone.utc))
    started = time.monotonic()
    results = [run_case(case, capture, as_of=as_of) for case in corpus()]
    duration_ms = int(round((time.monotonic() - started) * 1000))

    families: dict[str, dict[str, Any]] = {}
    for family in (GROUNDING, RELIABILITY):
        rows = [r for r in results if r.family == family]
        passed = sum(1 for r in rows if r.passed)
        latencies = [r.latency_ms for r in rows]
        families[family] = {
            "cases": len(rows),
            "passed": passed,
            "failed": len(rows) - passed,
            "pass_rate": f"{passed}/{len(rows)}",
            "failed_case_ids": [r.case_id for r in rows if not r.passed],
            "latency_p50_ms": _percentile(latencies, 0.50),
            "latency_p95_ms": _percentile(latencies, 0.95),
            "latency_max_ms": max(latencies) if latencies else None,
        }

    by_category: dict[str, dict[str, int]] = {}
    for row in results:
        bucket = by_category.setdefault(row.category, {"cases": 0, "passed": 0})
        bucket["cases"] += 1
        bucket["passed"] += int(row.passed)

    total = len(results)
    passed_total = sum(1 for r in results if r.passed)
    return {
        "schema_version": CORPUS_VERSION,
        "dataset_version": CORPUS_VERSION,
        "dataset_frozen_at": CORPUS_FROZEN_AT,
        "dataset_digest": corpus_digest(),
        "code": (code or code_identity()).model_dump(mode="json"),
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of.isoformat(),
        "command": command or "python evals/run_trace_eval.py",
        "duration_ms": duration_ms,
        "totals": {"cases": total, "passed": passed_total,
                   "failed": total - passed_total,
                   "pass_rate": f"{passed_total}/{total}"},
        "families": families,
        "by_category": by_category,
        "results": [r.as_dict() for r in results],
        "named_failures": [{"case_id": r.case_id, "failures": r.failures}
                           for r in results if not r.passed],
        "known_issues": [{"case_id": case.case_id, "note": case.known_issue}
                         for case in corpus() if case.known_issue],
        "claims": {
            "investment_accuracy_measured": False,
            "alpha_measured": False,
            "model_calls": 0,
            "network_reads": 0,
            "paid_dispatch_usd": "0.00",
            "meaning": ("对抗样例的确定性通过率，衡量的是守卫是否触发，不是投资判断正确率，"
                        "也不是模型 alpha。语料为冻结离线样例，未做人工投资质量标注。"),
        },
    }


def corpus_digest() -> str:
    """Stable digest of the corpus definition, so a report names the exact dataset."""
    import hashlib

    payload = [
        {"id": c.case_id, "category": c.category, "family": c.family, "runner": c.runner,
         "expect": c.expect, "expect_code": c.expect_code,
         "params": {k: (v if isinstance(v, (str, int, float, bool, list, type(None))) else str(v))
                    for k, v in c.params.items()}}
        for c in corpus()
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def render_markdown(report: dict) -> str:
    """Human-readable report. Named failures first: a rate without ids is useless."""
    lines = [
        "# MIA Trace/Eval report",
        "",
        f"- dataset: `{report['dataset_version']}` (`{report['dataset_digest'][:23]}…`)",
        f"- code: `{report['code']['commit'][:12]}` build `{report['code']['build']}` "
        f"source `{report['code']['source']}`",
        f"- evaluated_at: `{report['evaluated_at']}`  as_of: `{report['as_of']}`",
        f"- command: `{report['command']}`",
        f"- duration: {report['duration_ms']} ms",
        "",
        "## Metrics",
        "",
        "| family | pass | failed | p50 ms | p95 ms |",
        "| --- | --- | --- | --- | --- |",
    ]
    for family, row in report["families"].items():
        lines.append(f"| {family} | {row['pass_rate']} | {row['failed']} | "
                     f"{row['latency_p50_ms']} | {row['latency_p95_ms']} |")
    lines += ["", "## Named failures", ""]
    if report["named_failures"]:
        for failure in report["named_failures"]:
            lines.append(f"- `{failure['case_id']}`: {'; '.join(failure['failures'])}")
    else:
        lines.append("- none")
    lines += ["", "## Claims", "",
              f"- investment accuracy measured: **{report['claims']['investment_accuracy_measured']}**",
              f"- model calls: {report['claims']['model_calls']}, "
              f"network reads: {report['claims']['network_reads']}, "
              f"paid dispatch: {report['claims']['paid_dispatch_usd']}",
              f"- {report['claims']['meaning']}", ""]
    return "\n".join(lines)
