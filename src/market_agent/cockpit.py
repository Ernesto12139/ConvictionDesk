"""Natural-language front door over the existing Daily/Event/IDC workflows.

Framing is bounded deterministic extraction, not a universal conversational agent.
User assertions stay in an assumption namespace; only existing retrieval produces
evidence. Briefs project validated decisions without another model call.
"""
import asyncio
import re
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator, model_serializer

from .agent import QUERY, STOCK_EVENT_QUERY
from .analytics import fresh_observations
from .domain import AgentAnswer, FrozenModel, QueryRequest, RouteIntent, RoutePlan, RunTrace, utc
from .investment_context import ConditionedStatement, ScenarioAssumption
from .provider import MAX_EOD_AGE, ProviderError
from .tdb import InvestmentContextRequest, TDBError, content_hash
from .tdb_contract import EvidenceStatement
from .fast_decision import FastDecisionBrief

Objective = Literal["TACTICAL_ENTRY", "ADD_CAPITAL", "HOLD_REVIEW", "REDUCE_RISK",
                    "EVENT_IMPACT", "PORTFOLIO_RISK", "GENERAL_DECISION", "UNKNOWN"]
Workflow = Literal["IDC", "EVENT_DECISION", "DAILY", "EVENT_RESEARCH", "UNSUPPORTED", "CLARIFY"]
PriceSource = Literal["USER_INPUT", "COMPLETED_CLOSE", "UNAVAILABLE"]


class CockpitRequest(FrozenModel):
    query: str = Field(min_length=1, max_length=2000)
    symbol: str | None = None
    decision_price: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)

    @field_validator("query")
    @classmethod
    def nonempty_query(cls, value):
        if not value.strip():
            raise ValueError("empty question")
        return value.strip()

    @field_validator("symbol")
    @classmethod
    def symbol_format(cls, value):
        return InvestmentContextRequest.valid_symbol(value) if value else None


class UserAssertion(FrozenModel):
    assertion_id: str
    text: str = Field(min_length=1, max_length=500)
    source: Literal["USER_CONTEXT"] = "USER_CONTEXT"
    verification: Literal["UNVERIFIED"] = "UNVERIFIED"


class DecisionFraming(FrozenModel):
    version: Literal[1] = 1
    query: str
    symbol: str | None
    objective: Objective
    horizon: Literal["TACTICAL", "LONG_TERM", "UNKNOWN"]
    holding_status: Literal["HELD", "NOT_HELD", "UNKNOWN"]
    snapshot_id: str | None
    decision_price: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    price_source: PriceSource
    price_evidence_id: str | None = None
    price_as_of: datetime | None = None
    intent: Literal["CURRENT", "HYPOTHETICAL"]
    trigger: Literal["EVENT", "WHAT_CHANGED", "PRICE", "QUESTION"]
    assertions: tuple[UserAssertion, ...] = ()
    workflow: Workflow
    tool_plan: tuple[str, ...]
    research_query: str | None = None
    missing_critical: tuple[str, ...] = ()
    clarification: str | None = None
    _utc_price = field_validator("price_as_of")(lambda v: utc(v) if v else None)

    @model_validator(mode="after")
    def coherent_price_and_intent(self):
        if (self.decision_price is None) != (self.price_source == "UNAVAILABLE"):
            raise ValueError("price and source disagree")
        if self.price_source == "COMPLETED_CLOSE" and (not self.price_evidence_id or not self.price_as_of):
            raise ValueError("system price requires evidence and timestamp")
        if self.price_source != "COMPLETED_CLOSE" and (self.price_evidence_id or self.price_as_of):
            raise ValueError("user price cannot impersonate market evidence")
        if self.assertions and self.intent != "HYPOTHETICAL":
            raise ValueError("unverified assertions require conditional analysis")
        return self


class CockpitDecisionRequest(InvestmentContextRequest):
    """Internal only; the legacy public IDC request remains USER_INPUT-only."""
    source: Literal["USER_INPUT", "COMPLETED_CLOSE"] = "USER_INPUT"
    decision_framing: DecisionFraming

    @model_validator(mode="after")
    def framing_matches(self):
        f = self.decision_framing
        if (f.symbol != self.symbol or f.decision_price != self.decision_price or
                f.price_source != self.source or f.intent != self.intent):
            raise ValueError("decision framing and internal request disagree")
        return self


class DecisionCockpitBrief(FrozenModel):
    version: Literal[1] = 1
    primary_action: Literal["ADD", "HOLD", "REDUCE", "EXIT", "RESEARCH_FIRST",
        "INSUFFICIENT_EVIDENCE", "REVIEW_CONCENTRATION", "EVENT_RESEARCH",
        "CAPABILITY_UNAVAILABLE", "CLARIFY"]
    # The existing judge does not assess sizing or semantic action strength.
    action_strength: Literal["UNKNOWN"] = "UNKNOWN"
    one_line_decision: ConditionedStatement | None = None
    top_reasons: tuple[EvidenceStatement, ...] = Field(default=(), max_length=3)
    main_counterargument: EvidenceStatement | None = None
    what_changes_my_mind: tuple[EvidenceStatement, ...] = Field(default=(), max_length=3)
    next_best_check: EvidenceStatement | None = None
    decision_evidence_ids: tuple[str, ...] = Field(default=(), max_length=4)
    boundary: str | None = None


class CockpitResult(FrozenModel):
    version: Literal[1, 2] = 1
    cockpit_id: str
    created_at: datetime
    framing: DecisionFraming
    brief: DecisionCockpitBrief | FastDecisionBrief
    decision_id: str | None = None
    run_id: str | None = None
    checkpoint_id: str | None = None
    record_hash: str
    _utc = field_validator("created_at")(utc)

    @model_serializer(mode="wrap")
    def preserve_old_bytes(self, handler):
        payload = handler(self)
        if self.version == 1 and "checkpoint_id" not in self.model_fields_set:
            payload.pop("checkpoint_id", None)
        return payload

    @model_validator(mode="after")
    def integrity(self):
        if self.version == 2 and (not self.checkpoint_id or not isinstance(self.brief, FastDecisionBrief) or self.decision_id):
            raise ValueError("Fast result requires a checkpoint and separate Deep record")
        if self.record_hash != content_hash(self.model_dump(mode="json", exclude={"record_hash"})):
            raise ValueError("cockpit result hash mismatch")
        return self


# ASCII lookarounds allow tickers adjacent to Chinese words. Prices require a
# price cue; years, dates, percentages and quantities cannot silently become USD.
PRICE_CUE = r"(?:跌到|跌至|涨到|涨至|回到|到达|现价(?:为)?|价格(?:为|是)?|报价|判断价|股价(?:为|是)?|\$)\s*"
PRICE = re.compile(PRICE_CUE + r"([+-]?(?:\d[\d,]*(?:\.\d+)?|\.\d+))(?![\d.%％A-Za-z])", re.I)
NUMERIC_PRICE_CUE = re.compile(PRICE_CUE + r"(?=[+-]?(?:\d|\.\d))", re.I)
EVENT_TERMS = ("官司", "诉讼", "事件", "新闻", "财报", "监管", "合同", "指引", "并购", "诉讼", "lawsuit", "earnings")
DISCOVERY_TERMS = ("找三只", "找几只", "找新股票", "推荐股票", "推荐三只", "选股", "新股票", "discover", "stock picks")


def frame(request: CockpitRequest, snapshot) -> DecisionFraming:
    query = request.query
    lower = query.lower()
    held = {p.symbol for p in snapshot.positions} if snapshot else set()
    candidates = set(re.findall(r"(?<![A-Za-z0-9])\$?([A-Z][A-Z0-9]*(?:[.\-][A-Z]+)?)(?![A-Za-z0-9])", query))
    for symbol in held:
        if re.search(r"(?<![a-z0-9])" + re.escape(symbol.lower()) + r"(?![a-z0-9])", lower):
            candidates.add(symbol)
    if "applovin" in lower: candidates.add("APP")
    if "blackberry" in lower or "黑莓" in query: candidates.add("BB")
    candidates -= {"USD", "SEC", "PE", "TTM", "AI"}
    known_candidates = candidates & held
    # A second company mentioned in an event is not necessarily the subject.
    symbols = known_candidates if len(known_candidates) == 1 else candidates
    symbol = request.symbol or (next(iter(symbols)) if len(symbols) == 1 else None)
    objective: Objective = "UNKNOWN"
    horizon = "TACTICAL" if any(t in lower for t in ("今天", "短线", "反弹", "日内", "today", "short-term")) else (
        "LONG_TERM" if any(t in lower for t in ("长期", "核心仓", "long-term")) or
        re.search(r"(?<!\d)\d{1,2}(?:\s*[–\-到至~]\s*\d{1,2})?\s*年(?!度)", query) else "UNKNOWN")
    event = any(t in lower for t in EVENT_TERMS)
    changed = any(t in lower for t in ("什么变化", "有什么变化", "横盘", "继续拿", "继续持有", "还值得", "还要拿"))
    if any(t in lower for t in ("减仓", "减持", "卖出", "清仓", "降低风险", "reduce", "sell")):
        objective = "REDUCE_RISK"
    elif changed or any(t in lower for t in ("持仓信心", "继续持有", "hold")):
        objective = "HOLD_REVIEW"
    elif any(t in lower for t in ("反弹", "短线", "日内")):
        objective = "TACTICAL_ENTRY"
    elif any(t in lower for t in ("加仓", "增持", "抄底", "买入", "值得买", "买不买", "能不能买", "add", "buy")):
        objective = "ADD_CAPITAL"
    elif event:
        objective = "EVENT_IMPACT"
    elif any(t in lower for t in ("怎么办", "判断", "决定")):
        objective = "GENERAL_DECISION"

    explicit_scenario = any(t in lower for t in ("假设", "如果", "假如", "若是", "suppose", "what if"))
    assertions = []
    for clause in re.split(r"[，,。！？!?;；\n]", query):
        clause = clause.strip()
        # Questions about an event do not assert that a specific outcome occurred.
        if (any(t in clause.lower() for t in EVENT_TERMS) and
            not any(t in clause for t in ("会不会", "是否", "有什么", "如何", "怎么", "多大", "吗"))):
            if clause:
                assertions.append(UserAssertion(assertion_id=f"U:{len(assertions)+1}", text=clause[:450]))
    if explicit_scenario and not assertions:
        assertions.append(UserAssertion(assertion_id="U:1", text=query[:450]))
    assertions = assertions[:5]
    matches = tuple(PRICE.finditer(query))
    prices = tuple(dict.fromkeys(m.group(1).replace(",", "") for m in matches))
    malformed_price = {m.start() for m in NUMERIC_PRICE_CUE.finditer(query)} - {m.start() for m in matches}
    price = request.decision_price
    missing = []
    clarification = None
    if price is None and malformed_price:
        missing.append("VALID_PRICE"); clarification = "请用普通十进制填写一个美元判断价格，例如 245。"
    elif price is None and len(prices) == 1:
        price = Decimal(prices[0])
        if not price.is_finite() or price <= 0:
            price = None; missing.append("VALID_PRICE")
            clarification = "请提供大于零的美元判断价格。"
    elif price is None and len(prices) > 1:
        missing.append("ONE_PRICE"); clarification = "这次按哪个美元价格判断？可在修正信息中填写。"
    workflow: Workflow = "EVENT_DECISION" if event else "IDC"
    plan = ("portfolio.get_snapshot", "market.get_eod", "thesis.get", "decision.get_previous_review",
            "news.search", "sec.search", "decision.judge")
    if any(t in lower for t in DISCOVERY_TERMS):
        workflow = "UNSUPPORTED"; plan = (); objective = "UNKNOWN"
        clarification = "MIA 尚未实现新股票发现。可以输入已有持仓，检查事件影响或加减仓判断。"
    elif "组合" in query and any(t in lower for t in ("风险", "集中", "持仓分布")) and not symbol:
        workflow = "DAILY"; objective = "PORTFOLIO_RISK"
        plan = ("portfolio.get_snapshot", "market.get_eod", "portfolio.compute_analytics")
        if snapshot is None:
            missing.append("PORTFOLIO"); clarification = "请先建立组合，再检查持仓集中风险。"
    elif symbol == "BB" and symbol not in held and event and objective == "EVENT_IMPACT":
        workflow = "EVENT_RESEARCH"
        plan = ("news.search", "sec.search", "thesis.get", "semantic.assess_thesis_impact")
    elif not symbol:
        missing.append("SYMBOL"); clarification = "这次要判断哪只股票？在问题中写入一个代码，例如 APP。"
    elif symbol not in held:
        workflow = "UNSUPPORTED"; plan = ()
        clarification = "当前持仓决策需要已保存的持仓。请先录入该股票；新股票买入研究尚未实现。"
    if missing and workflow != "UNSUPPORTED":
        workflow = "CLARIFY"; plan = ()
    research_query = None
    if event:
        research_query = "litigation lawsuit legal risk" if any(t in lower for t in ("官司", "诉讼", "lawsuit")) else "recent event material impact guidance risk"
        if "unity" in lower: research_query = "Unity " + research_query
    return DecisionFraming(query=query, symbol=symbol, objective=objective, horizon=horizon,
        holding_status="HELD" if symbol in held else "NOT_HELD" if snapshot else "UNKNOWN",
        snapshot_id=snapshot.snapshot_id if snapshot else None, decision_price=price,
        price_source="USER_INPUT" if price is not None else "UNAVAILABLE",
        intent="HYPOTHETICAL" if assertions or explicit_scenario else "CURRENT",
        trigger="EVENT" if event else "WHAT_CHANGED" if changed else "PRICE" if price else "QUESTION",
        assertions=tuple(assertions), workflow=workflow, tool_plan=plan, research_query=research_query,
        missing_critical=tuple(missing), clarification=clarification)


def decision_request(framing: DecisionFraming) -> CockpitDecisionRequest:
    assumptions = tuple(ScenarioAssumption(assumption_id=f"A:user-{i+1}", kind="CAUSE",
        statement="仅按你提供的信息成立分析：" + assertion.text) for i, assertion in enumerate(framing.assertions))
    return CockpitDecisionRequest(symbol=framing.symbol, decision_price=framing.decision_price,
        source=framing.price_source, intent=framing.intent, decision_framing=framing,
        scenario_cause="OTHER_EXPLICIT_ASSUMPTION" if framing.intent == "HYPOTHETICAL" else None,
        assumptions=assumptions)


def project_decision(record) -> DecisionCockpitBrief:
    """Select existing validated statements; do not synthesize new financial claims."""
    semantic = record.semantic
    support = semantic.action_support
    primary = semantic.assessment.assessment_status
    if support:
        # Percentage normalization uses deterministic remainder allocation. That
        # display rounding must never manufacture a unique action from a tie.
        values = semantic.assessment.scores.values()
        leaders = [a for a, value in values.items() if value == max(values.values())]
        primary = leaders[0] if len(leaders) == 1 else "RESEARCH_FIRST"
    depth = semantic.depth
    reasons = []
    if primary in {"ADD", "HOLD", "REDUCE", "EXIT"}:
        rationale = next(r for r in semantic.assessment.rationales if r.action == primary)
        reasons.append(EvidenceStatement(text=rationale.rationale, evidence_ids=rationale.evidence_ids))
    if depth:
        reasons.extend((depth.price_implication, depth.business_quality))
    headline = record.conditional_assessment.headline if record.conditional_assessment else None
    changes = tuple(depth.change_my_mind) if depth else ()
    if record.conditional_assessment and record.investment_context.intent == "HYPOTHETICAL":
        conditioned = record.conditional_assessment.change_conditions
        changes = (EvidenceStatement(text=conditioned.text, evidence_ids=conditioned.evidence_ids), *changes)[:3]
    statements = [headline, *reasons, depth.key_risks[0] if depth else None,
                  *changes, depth.next_verification if depth else None]
    refs = tuple(dict.fromkeys(eid for s in statements if s for eid in s.evidence_ids))[:4]
    boundary = semantic.assessment.abstention_reason
    if support and primary == "RESEARCH_FIRST":
        boundary = "多种行动获得相同支持，先核查关键分歧再决定。"
        headline = None
    return DecisionCockpitBrief(primary_action=primary, one_line_decision=headline,
        top_reasons=tuple(reasons[:3]), main_counterargument=depth.key_risks[0] if depth else None,
        what_changes_my_mind=changes, next_best_check=depth.next_verification if depth else None,
        decision_evidence_ids=refs, boundary=boundary)


def save_result(store, framing, brief, now, *, decision_id=None, run_id=None):
    if run_id is None:
        run_id = str(uuid4())
        store.start_run(run_id, framing.query)
        trace = RunTrace(run_id=run_id, query=framing.query, requested_as_of=None,
            route_plan=RoutePlan(intent=RouteIntent.UNSUPPORTED, required_tools=[], needs_semantic_judgment=False),
            tool_calls=[], synthesis=None, evidence_ids=[], final_claim_links={})
        store.finish_run(trace, AgentAnswer(run_id=run_id, answer=brief.boundary or "请补充问题。",
            claims=[], evidence=[], limitations=[]))
    fields = dict(cockpit_id=str(uuid4()), created_at=utc(now), framing=framing,
                  brief=brief, decision_id=decision_id, run_id=run_id)
    payload = CockpitResult.model_construct(**fields).model_dump(mode="json", exclude={"record_hash"})
    result = CockpitResult(**fields, record_hash=content_hash(payload))
    store.add_cockpit_result(result)
    return result


async def execute(framing, agent, judge, *, progress=None, quote_provider=None, fast_judge=None):
    """One existing workflow, with no new semantic stage or evidence pipeline."""
    store, clock = agent.store, agent.clock
    if framing.workflow in {"CLARIFY", "UNSUPPORTED"}:
        return save_result(store, framing, DecisionCockpitBrief(primary_action=
            "CLARIFY" if framing.workflow == "CLARIFY" else "CAPABILITY_UNAVAILABLE",
            boundary=framing.clarification), clock())
    if framing.workflow in {"DAILY", "EVENT_RESEARCH"}:
        if progress: progress("research", {})
        answer = await agent.query(QueryRequest(query=QUERY if framing.workflow == "DAILY" else STOCK_EVENT_QUERY))
        return save_result(store, framing, DecisionCockpitBrief(primary_action=
            "REVIEW_CONCENTRATION" if framing.workflow == "DAILY" else "EVENT_RESEARCH",
            decision_evidence_ids=tuple(dict.fromkeys(eid for claim in answer.claims for eid in claim.evidence_ids))[:4],
            boundary="当前覆盖持仓与行业集中风险。" if framing.workflow == "DAILY" else "未保存该股持仓，本次仅研究公开事件。"),
            clock(), run_id=answer.run_id)
    market = agent.market
    if framing.decision_price is None:
        # Reuse the very same normalized result in foundation construction. It still
        # passes all incumbent symbol/basis/freshness/PIT validation there.
        from .tdb import _observation_matches_evidence, _pit_eligible
        now = utc(clock())
        try:
            result = await asyncio.to_thread(market.retrieve_eod, [framing.symbol], now)
        except ProviderError:
            result = None
        eligible = []
        if result:
            by_id = {i.evidence_id: i for i in result.evidence}
            observations, _ = fresh_observations(result.observations, now, MAX_EOD_AGE)
            eligible = [o for o in observations if o.symbol == framing.symbol and o.evidence_id in by_id
                and _observation_matches_evidence(o, by_id[o.evidence_id])
                and _pit_eligible(by_id[o.evidence_id], utc(clock()), historical=False)]
        close = max(eligible, key=lambda o: (o.as_of, o.evidence_id), default=None)
        if close is None:
            f = framing.model_copy(update={"workflow":"CLARIFY", "missing_critical":("DECISION_PRICE",),
                "clarification":"没有合资格的收盘参考。请补充一个美元判断价格，便可继续分析。"})
            return save_result(store, f, DecisionCockpitBrief(primary_action="CLARIFY", boundary=f.clarification), clock())
        framing = framing.model_copy(update={"decision_price":close.price, "price_source":"COMPLETED_CLOSE",
            "price_evidence_id":close.evidence_id, "price_as_of":close.as_of})
        market = TargetEODReuse(market, framing.symbol, result)
        if progress: progress("framing", {"framing":framing.model_dump(mode="json")})
    if fast_judge:
        from .tdb_workflow import build
        from .fast_decision import run_fast
        checkpoint = await build(decision_request(framing), store, market, agent.news, agent.sec,
            judge, clock, progress=progress, quote_provider=quote_provider, prepare_only=True)
        brief, run_id = await run_fast(store, checkpoint, fast_judge, clock, progress=progress)
        return save_fast_result(store, framing, brief, clock(), checkpoint, run_id)
    from .tdb_semantic import build_semantic
    record = await build_semantic(decision_request(framing), store, market, agent.news, agent.sec,
        judge, clock, progress=progress, quote_provider=quote_provider)
    return save_result(store, framing, project_decision(record), clock(),
                       decision_id=record.decision_id, run_id=record.run_id)


def save_fast_result(store, framing, brief, now, checkpoint_id, run_id):
    fields = dict(version=2, cockpit_id=str(uuid4()), created_at=utc(now), framing=framing,
        brief=brief, checkpoint_id=checkpoint_id, run_id=run_id)
    payload = CockpitResult.model_construct(**fields).model_dump(mode="json", exclude={"record_hash"})
    result = CockpitResult(**fields, record_hash=content_hash(payload))
    store.add_cockpit_result(result)
    return result


class TargetEODReuse:
    """Single-run reuse, not a new market cache or an alternate provider."""
    def __init__(self, market, symbol, result):
        self.market, self.symbol, self.result = market, symbol, result
        self.name = market.name
    def retrieve_eod(self, symbols, cutoff):
        if symbols == [self.symbol]:
            return self.result
        return self.market.retrieve_eod(symbols, cutoff)
    def __getattr__(self, name):
        return getattr(self.market, name)
