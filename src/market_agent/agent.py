"""Deterministic first-slice route, tool execution, grounding and trace."""

import hashlib
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .analytics import ConcentrationMetrics, compute_analytics, fresh_observations
from .clock import Clock, system_utc_now
from .domain import (AgentAnswer, AnalyticsToolArgs, EvidenceItem, GroundedClaim, MarketToolArgs,
                     NewsSearchToolArgs, PortfolioSnapshot, PriceObservation, QueryRequest,
                     RouteIntent, RoutePlan, RunTrace, SECSearchToolArgs, SemanticToolArgs,
                     SnapshotToolArgs, SynthesisEvent, ThesisToolArgs, ToolCall, utc)
from .persistence import Store
from .provider import EODProvider, MAX_EOD_AGE, ProviderError
from .llm import LLMClient, LLMError, StructuredLLMRequest, observed_model_event
from .news import NewsProvider
from .sec import SECEDGARProvider
from .semantic import LLMSemanticJudge
from .thesis import AIThesisGenerator, ThesisProfile


QUERY = "现在最大的集中风险在哪里？"
TOOLS = ["portfolio.get_snapshot", "market.get_eod", "portfolio.compute_analytics"]
STOCK_EVENT_QUERY = "BB 最近发生的事情是否改变了投资逻辑？"
STOCK_EVENT_TOOLS = [
    "portfolio.get_snapshot",
    "news.search",
    "sec.search",
    "thesis.get",
    "semantic.assess_thesis_impact",
]
NUMBER_TOKEN = re.compile(
    r"(?<!\w)[-+]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)"
    r"(?:[eE][+-]?\d+)?%?(?!\w)"
)


class AgentRunError(ValueError):
    def __init__(self, run_id: str, message: str, *, provider_code: str | None = None):
        super().__init__(message)
        self.run_id = run_id
        self.provider_code = provider_code


def payload_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def route(request: QueryRequest) -> RoutePlan:
    query = re.sub(r"[\s，。！？?,.!]+", "", request.query).lower()
    action_terms = ("怎么买", "怎么卖", "该买吗", "该卖吗", "买入", "卖出", "减仓", "加仓",
                    "推荐", "值得买", "如何降低", "怎么降低", "如何调整", "模拟")
    asks_for_action = any(term in query for term in action_terms)
    concentration = any(term in query for term in
                        ("集中风险", "持仓集中", "组合集中", "仓位集中", "行业集中"))
    blackberry = bool(re.search(r"(?<![a-z])bb(?![a-z])|blackberry", request.query.lower()))
    event = any(term in query for term in ("事件", "发生", "新闻", "投资逻辑"))
    if not asks_for_action and concentration and not blackberry:
        return RoutePlan(intent=RouteIntent.DAILY_PORTFOLIO, requested_as_of=request.as_of,
                         required_tools=TOOLS, needs_semantic_judgment=False, confidence="high")
    if not asks_for_action and blackberry and event:
        return RoutePlan(
            intent=RouteIntent.STOCK_EVENT_THESIS,
            symbols=["BB"],
            requested_as_of=request.as_of,
            required_tools=STOCK_EVENT_TOOLS,
            needs_semantic_judgment=True,
            confidence="high",
        )
    return RoutePlan(intent=RouteIntent.UNSUPPORTED, requested_as_of=request.as_of,
                     required_tools=[], needs_semantic_judgment=False, confidence="high")


class SynthesisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    claims: list[GroundedClaim]
    limitations: list[str]


def allowed_grounded_syntheses(facts: dict) -> list[SynthesisResult]:
    evidence_id = facts["calculation_evidence_id"]
    if facts["top_holding"] is None:
        answers = [
            "当前没有可定价持仓，无法判断最大集中风险。",
            "缺少可定价持仓，当前无法识别最大集中风险。",
        ]
    else:
        top = facts["top_holding"]
        sector = facts["top_sector"]
        sector_text = f"行业集中度最高为 {sector['name']}（占已定价权益 {sector['weight']}）；" if sector else ""
        first = (sector_text + f"最大单一持仓集中在 {top['symbol']}（{top['position_id']}），"
                 f"占 NAV {top['nav_weight']}；前三持仓占 NAV {facts['top_three_nav_weight']}，"
                 f"HHI 为 {facts['hhi']}。")
        holding_text = (f"{top['symbol']}（{top['position_id']}）是最大的单一持仓，"
                        f"占 NAV {top['nav_weight']}。")
        second = holding_text
        if sector:
            second += f"{sector['name']} 是集中度最高的行业，占已定价权益 {sector['weight']}。"
        second += f"前三持仓占 NAV {facts['top_three_nav_weight']}；HHI 为 {facts['hhi']}。"
        answers = [first, second]
    return [
        SynthesisResult(
            answer=answer,
            claims=[GroundedClaim(claim_id="C1", claim_type="calculation", text=answer,
                                  evidence_ids=[evidence_id])],
            limitations=facts["limitations"],
        )
        for answer in answers
    ]


def expected_grounded_synthesis(facts: dict) -> SynthesisResult:
    return allowed_grounded_syntheses(facts)[0]


def _numeric_tokens(value: object) -> set[str]:
    if isinstance(value, str):
        return set(NUMBER_TOKEN.findall(value))
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return {str(value)}
    if isinstance(value, dict):
        tokens: set[str] = set()
        for item in value.values():
            tokens.update(_numeric_tokens(item))
        return tokens
    if isinstance(value, (list, tuple)):
        tokens = set()
        for item in value:
            tokens.update(_numeric_tokens(item))
        return tokens
    return set()


def validate_synthesis(result: SynthesisResult, request: StructuredLLMRequest) -> None:
    allowed_ids = set(request.evidence_ids)
    if any(set(claim.evidence_ids) - allowed_ids for claim in result.claims):
        raise LLMError(
            "invalid_evidence_reference",
            "LLM claim cites evidence outside the synthesis input",
        )
    allowed = [request.response_model.model_validate(item) for item in request.grounded_responses]
    if result in allowed:
        return
    output_text = [result.answer, *[claim.text for claim in result.claims], *result.limitations]
    expected_text = [
        text
        for expected in allowed
        for text in [expected.answer, *[claim.text for claim in expected.claims], *expected.limitations]
    ]
    allowed_for_classification = set(request.numeric_fact_tokens) | _numeric_tokens(expected_text)
    if _numeric_tokens(output_text) - allowed_for_classification:
        raise LLMError(
            "unsupported_numeric_fact",
            "LLM output introduced a numeric token absent from deterministic facts",
        )
    raise LLMError(
        "unsupported_claim",
        "LLM output changed the deterministic grounded response",
    )


async def complete_grounded_synthesis(
    llm: LLMClient, request: StructuredLLMRequest
) -> SynthesisResult:
    result = await llm.complete_structured(request)
    if not isinstance(result, SynthesisResult):
        raise LLMError("schema_validation", "LLM returned the wrong structured result type")
    validate_synthesis(result, request)
    return result


class MockLLMClient:
    model_name = "mock-deterministic"

    async def complete_structured(self, request: StructuredLLMRequest) -> BaseModel:
        if not request.grounded_responses:
            raise ValueError("mock request requires a deterministic grounded response")
        return request.response_model.model_validate(request.grounded_responses[0])


def snapshot_evidence(snapshot: PortfolioSnapshot, retrieved_at: datetime) -> EvidenceItem:
    payload = {"snapshot_id": snapshot.snapshot_id, "content_hash": snapshot.content_hash}
    return EvidenceItem(evidence_id="E" + payload_hash(payload)[7:23], evidence_type="portfolio_snapshot",
                        provider="portfolio_repository", source_record_id=snapshot.snapshot_id,
                        retrieved_at=retrieved_at, as_of=snapshot.as_of, content_hash=payload_hash(payload),
                        structured_data=payload)


def calculation_evidence(snapshot: PortfolioSnapshot, market_cutoff: datetime,
                         observations: list[PriceObservation], market_ids: list[str],
                         metrics: ConcentrationMetrics, retrieved_at: datetime) -> EvidenceItem:
    payload = {"calculation": "portfolio_concentration", "version": 1,
               "snapshot_id": snapshot.snapshot_id, "snapshot_hash": snapshot.content_hash,
               "portfolio_as_of": snapshot.as_of.isoformat(),
               "market_cutoff": market_cutoff.isoformat(),
               "market_observation_as_of": {
                   observation.symbol: observation.as_of.isoformat()
                   for observation in observations
               },
               "market_evidence_ids": market_ids, "result": metrics.model_dump(mode="json")}
    digest = payload_hash(payload)
    return EvidenceItem(evidence_id="E" + digest[7:23], evidence_type="calculation",
                        provider="deterministic_python", title="Portfolio concentration",
                        retrieved_at=retrieved_at, as_of=market_cutoff, content_hash=digest,
                        structured_data=payload)


class Agent:
    def __init__(self, store: Store, market: EODProvider, llm: LLMClient | None = None,
                 clock: Clock | None = None, news: NewsProvider | None = None,
                 sec: SECEDGARProvider | None = None, semantic_judge=None,
                 comparison_semantic_judge=None):
        self.store = store
        self.market = market
        self.llm = llm or MockLLMClient()
        self.clock = clock or system_utc_now
        self.news = news
        self.sec = sec
        self.thesis_generator = AIThesisGenerator(store, self.llm, clock=self.clock)
        self.semantic_judge = semantic_judge or LLMSemanticJudge(self.llm, clock=self.clock)
        self.comparison_semantic_judge = comparison_semantic_judge

    def _now(self) -> datetime:
        return utc(self.clock())

    def create_tdb_foundation(self, request):
        """Explicit TDB entry; the natural-language router is intentionally unchanged."""
        from .tdb import build_foundation

        return build_foundation(request, self.store, self.market, self.clock)

    async def query(self, request: QueryRequest) -> AgentAnswer:
        run_id = str(uuid4())
        plan = route(request)
        market_cutoff = request.as_of or self._now()
        self.store.start_run(run_id, request.query)
        if plan.intent is RouteIntent.STOCK_EVENT_THESIS:
            return await self._query_stock_event(request, run_id, plan, market_cutoff)
        calls: list[ToolCall] = []
        evidence: list[EvidenceItem] = []
        synthesis = None
        active_tool = None
        active_started = None
        active_args = None

        def trace(claims: list[GroundedClaim] | None = None) -> RunTrace:
            return RunTrace(run_id=run_id, query=request.query, requested_as_of=request.as_of,
                            route_plan=plan, tool_calls=calls, synthesis=synthesis,
                            evidence_ids=[item.evidence_id for item in evidence],
                            final_claim_links={claim.claim_id: claim.evidence_ids for claim in claims or []})

        if plan.intent is RouteIntent.UNSUPPORTED:
            answer = AgentAnswer(
                run_id=run_id,
                answer="这个问题目前还没有对应的已验证分析工具。当前支持组合集中风险分析，以及 BlackBerry 事件对投资逻辑的影响研究。",
                claims=[], evidence=[], limitations=[])
            self.store.finish_run(trace(), answer)
            return answer

        try:
            started = self._now()
            active_tool, active_started = TOOLS[0], started
            active_args = SnapshotToolArgs(as_of=request.as_of)
            snapshot = self.store.get_snapshot(request.as_of)
            if snapshot is None:
                raise ValueError("INSUFFICIENT_HISTORY: no portfolio snapshot at requested cutoff")
            item = snapshot_evidence(snapshot, self._now())
            evidence.append(item)
            calls.append(ToolCall(sequence=1, name=TOOLS[0], arguments=active_args,
                                  status="success", started_at=started, ended_at=self._now(), output_refs=[snapshot.snapshot_id, item.evidence_id]))
            active_tool = None

            symbols = sorted({position.symbol for position in snapshot.positions})
            started = self._now()
            active_tool, active_started = TOOLS[1], started
            active_args = MarketToolArgs(symbols=symbols, as_of=market_cutoff)
            retrieved = self.market.retrieve_eod(symbols, market_cutoff)
            observations = retrieved.observations
            market_items = retrieved.evidence
            if any(observation.as_of > market_cutoff for observation in observations):
                raise ValueError("market observation exceeds requested cutoff")
            evidence_by_id = {item.evidence_id: item for item in market_items}
            if any(evidence_by_id[observation.evidence_id].as_of != observation.as_of
                   for observation in observations):
                raise ValueError("market evidence timestamp does not match observation")
            if any(item.published_at is not None and item.published_at > market_cutoff
                   for item in market_items):
                raise ValueError("market evidence was published after requested cutoff")
            if request.as_of is not None and any(
                item.retrieved_at > market_cutoff
                and (item.published_at is None or item.published_at > market_cutoff)
                for item in market_items
            ):
                raise ValueError(
                    "INSUFFICIENT_HISTORICAL_EVIDENCE: market-data vintage is not proven at requested cutoff"
                )
            observations, stale_symbols = fresh_observations(
                observations, market_cutoff, MAX_EOD_AGE
            )
            accepted_ids = [observation.evidence_id for observation in observations]
            evidence.extend(market_items)
            calls.append(ToolCall(sequence=2, name=TOOLS[1], arguments=active_args,
                                  status="success", started_at=started, ended_at=self._now(),
                                  output_refs=[item.evidence_id for item in market_items]))
            active_tool = None

            started = self._now()
            active_tool, active_started = TOOLS[2], started
            active_args = AnalyticsToolArgs(snapshot_id=snapshot.snapshot_id,
                                            price_evidence_ids=accepted_ids)
            metrics = compute_analytics(snapshot, observations)
            calc = calculation_evidence(
                snapshot, market_cutoff, observations, accepted_ids, metrics, self._now()
            )
            evidence.append(calc)
            calls.append(ToolCall(sequence=3, name=TOOLS[2], arguments=active_args,
                                  status="success", started_at=started, ended_at=self._now(), output_refs=[calc.evidence_id]))
            active_tool = None
            limitations = ([f"部分持仓缺少有效日终价格：{', '.join(metrics.missing_price_positions)}；集中度仅覆盖已定价部分。"]
                           if metrics.missing_price_positions else [])
            if stale_symbols:
                limitations.insert(
                    0,
                    f"Stale EOD price for {', '.join(stale_symbols)} exceeded the "
                    f"{MAX_EOD_AGE.days}-day evidence window and was excluded.",
                )
            top_sector = max(metrics.sector_weights.items(), key=lambda item: (item[1], item[0])) if metrics.sector_weights else None
            facts = {"top_holding": metrics.top_holding.model_dump(mode="json") if metrics.top_holding else None,
                     "top_sector": ({"name": top_sector[0], "weight": str(top_sector[1])} if top_sector else None),
                     "top_three_nav_weight": str(metrics.top_three_nav_weight), "hhi": str(metrics.hhi),
                     "limitations": limitations, "calculation_evidence_id": calc.evidence_id}
            started = self._now()
            active_started = started
            grounded_responses = allowed_grounded_syntheses(facts)
            llm_request = StructuredLLMRequest(
                purpose="grounded_concentration_synthesis", response_model=SynthesisResult,
                facts=facts, evidence_ids=[item.evidence_id for item in evidence],
                numeric_fact_tokens=[
                    *(
                        [
                            str(metrics.top_holding.value),
                            str(metrics.top_holding.nav_weight),
                            str(metrics.top_holding.unrealized_pnl),
                        ]
                        if metrics.top_holding else []
                    ),
                    *([str(top_sector[1])] if top_sector else []),
                    str(metrics.top_three_nav_weight),
                    str(metrics.hhi),
                    *([str(MAX_EOD_AGE.days)] if stale_symbols else []),
                ],
                grounded_responses=[item.model_dump(mode="json") for item in grounded_responses])
            result = await complete_grounded_synthesis(self.llm, llm_request)
            answer = AgentAnswer(run_id=run_id, answer=result.answer, claims=result.claims,
                                 evidence=evidence, limitations=result.limitations)
            synthesis = observed_model_event(self.llm, model=self.llm.model_name, purpose="grounded_concentration_synthesis",
                                       started_at=started, ended_at=self._now(),
                                       evidence_ids=[item.evidence_id for item in evidence],
                                       claim_links={claim.claim_id: claim.evidence_ids for claim in answer.claims})
            self.store.finish_run(trace(answer.claims), answer)
            return answer
        except Exception as exc:
            if active_tool is not None:
                calls.append(ToolCall(sequence=len(calls) + 1, name=active_tool, arguments=active_args,
                                      status="error", started_at=active_started, ended_at=self._now(), error=str(exc)))
            elif len(calls) == len(TOOLS):
                synthesis = observed_model_event(self.llm, model=self.llm.model_name, purpose="grounded_concentration_synthesis",
                                           status="error", started_at=active_started, ended_at=self._now(),
                                           evidence_ids=[item.evidence_id for item in evidence],
                                           claim_links={}, error=str(exc))
            self.store.fail_run(run_id, trace())
            if isinstance(exc, ProviderError):
                raise AgentRunError(run_id, str(exc), provider_code=exc.code) from exc
            if isinstance(exc, ValueError):
                raise AgentRunError(run_id, str(exc)) from exc
            raise

    async def _query_stock_event(
        self, request: QueryRequest, run_id: str, plan: RoutePlan, cutoff: datetime
    ) -> AgentAnswer:
        calls: list[ToolCall] = []
        evidence: list[EvidenceItem] = []
        model_events: list[SynthesisEvent] = []
        synthesis = None
        active_tool = None
        active_started = None
        active_args = None
        active_model = None
        active_model_identity = None
        active_model_started = None
        active_model_evidence: list[str] = []

        def trace(claims: list[GroundedClaim] | None = None) -> RunTrace:
            return RunTrace(
                run_id=run_id,
                query=request.query,
                requested_as_of=request.as_of,
                route_plan=plan,
                tool_calls=calls,
                synthesis=synthesis,
                model_events=model_events,
                evidence_ids=[item.evidence_id for item in evidence],
                final_claim_links={claim.claim_id: claim.evidence_ids for claim in claims or []},
            )

        def add_call(name: str, args, started: datetime, refs: list[str]) -> None:
            calls.append(ToolCall(
                sequence=len(calls) + 1,
                name=name,
                arguments=args,
                status="success",
                started_at=started,
                ended_at=self._now(),
                output_refs=refs,
            ))

        try:
            symbol = plan.symbols[0]

            started = self._now()
            active_tool, active_started = STOCK_EVENT_TOOLS[0], started
            active_args = SnapshotToolArgs(as_of=request.as_of)
            snapshot = self.store.get_snapshot(request.as_of)
            portfolio_item = snapshot_evidence(snapshot, self._now()) if snapshot else None
            if portfolio_item:
                evidence.append(portfolio_item)
            add_call(
                active_tool,
                active_args,
                started,
                ([snapshot.snapshot_id, portfolio_item.evidence_id] if snapshot else []),
            )
            active_tool = None

            if self.news is None:
                raise ProviderError("configuration", "news provider is not configured")
            since = cutoff - timedelta(days=90)
            started = self._now()
            active_tool, active_started = STOCK_EVENT_TOOLS[1], started
            active_args = NewsSearchToolArgs(
                symbols=[symbol], since=since, until=cutoff, limit=5
            )
            news_result = self.news.search([symbol], since, cutoff, 5)
            news_items = news_result.evidence
            self._validate_event_evidence(news_items, symbol, cutoff)
            evidence.extend(news_items)
            add_call(active_tool, active_args, started, [item.evidence_id for item in news_items])
            active_tool = None

            if self.sec is None:
                raise ProviderError("configuration", "SEC provider is not configured")
            started = self._now()
            active_tool, active_started = STOCK_EVENT_TOOLS[2], started
            active_args = SECSearchToolArgs(
                symbol=symbol,
                query="revenue cybersecurity strategy customers execution risk",
                forms=["10-K", "10-Q", "8-K", "20-F", "40-F", "6-K"],
                as_of=cutoff,
                limit=3,
            )
            sec_result = self.sec.search(
                symbol, active_args.query, active_args.forms, cutoff, active_args.limit
            )
            sec_items = sec_result.evidence
            self._validate_event_evidence(sec_items, symbol, cutoff)
            evidence.extend(sec_items)
            add_call(active_tool, active_args, started, [item.evidence_id for item in sec_items])
            active_tool = None

            retrieved_items = self._deduplicate_evidence([*news_items, *sec_items])
            started = self._now()
            active_tool, active_started = STOCK_EVENT_TOOLS[3], started
            active_args = ThesisToolArgs(symbol=symbol, as_of=cutoff)
            thesis: ThesisProfile | None = self.store.get_thesis(symbol, cutoff)
            thesis_item = (
                self.store.get_thesis_evidence(thesis.thesis_version_id) if thesis else None
            )
            generated_thesis = False
            if thesis is None:
                thesis_inputs, event_items = self._partition_initial_evidence(retrieved_items)
            else:
                thesis_inputs = []
                event_items = [
                    item
                    for item in retrieved_items
                    if item.evidence_id not in thesis.evidence_ids
                    and item.published_at is not None
                    and item.published_at > thesis.as_of
                ]
            if thesis is None and thesis_inputs and event_items:
                active_model = "ai_thesis_draft"
                active_model_identity = self.llm.model_name
                active_model_started = self._now()
                active_model_evidence = [item.evidence_id for item in thesis_inputs]
                thesis_as_of = max(item.published_at for item in thesis_inputs)
                try:
                    thesis, thesis_item = await self.thesis_generator.generate(
                        symbol, thesis_inputs, thesis_as_of, persist=False
                    )
                except LLMError as exc:
                    if exc.code not in {
                        "unsupported_claim", "unsupported_numeric_fact",
                        "invalid_evidence_reference", "schema_validation",
                    }:
                        raise
                    model_events.append(observed_model_event(self.llm,
                        model=self.llm.model_name,
                        purpose="ai_thesis_draft",
                        status="error",
                        started_at=active_model_started,
                        ended_at=self._now(),
                        evidence_ids=active_model_evidence,
                        claim_links={},
                        error=str(exc),
                    ))
                    active_model = "ai_thesis_draft_grounded_retry"
                    active_model_started = self._now()
                    thesis, thesis_item = await self.thesis_generator.generate(
                        symbol, thesis_inputs, thesis_as_of, persist=False,
                        conservative=True,
                    )
                generated_thesis = True
                model_events.append(observed_model_event(self.llm,
                    model=self.llm.model_name,
                    purpose=active_model,
                    started_at=active_model_started,
                    ended_at=self._now(),
                    evidence_ids=active_model_evidence,
                    claim_links={
                        f"thesis_statement_{index}": statement.evidence_ids
                        for index, statement in enumerate([
                            *thesis.core_drivers,
                            *thesis.key_assumptions,
                            *thesis.execution_risks,
                            *thesis.invalidation_conditions,
                        ], start=1)
                    },
                ))
                active_model = None
                active_model_identity = None
            if thesis is not None and thesis_item is None:
                raise ValueError("thesis evidence is missing for the selected thesis version")
            if thesis_item:
                evidence.append(thesis_item)
            add_call(
                active_tool,
                active_args,
                started,
                [thesis.thesis_version_id, thesis_item.evidence_id] if thesis and thesis_item else [],
            )
            active_tool = None

            started = self._now()
            active_tool, active_started = STOCK_EVENT_TOOLS[4], started
            active_args = SemanticToolArgs(
                symbol=symbol,
                thesis_version_id=thesis.thesis_version_id if thesis else None,
                evidence_ids=[item.evidence_id for item in event_items],
            )
            if thesis is None or not event_items:
                calls.append(ToolCall(
                    sequence=len(calls) + 1,
                    name=active_tool,
                    arguments=active_args,
                    status="error",
                    started_at=started,
                    ended_at=self._now(),
                    error=(
                        "INSUFFICIENT_EVIDENCE: independent thesis baseline and "
                        "new-event evidence are required"
                    ),
                ))
                active_tool = None
                limitations = [
                    "截至本次分析时间，缺少相互独立的投资逻辑基线与新事件证据。"
                ]
                answer = AgentAnswer(
                    run_id=run_id,
                    answer="截至指定时间，没有足够的合资格公开证据来判断 BB 的投资逻辑是否改变。",
                    claims=[],
                    evidence=self._deduplicate_evidence(evidence),
                    limitations=limitations,
                )
                synthesis = observed_model_event(self.llm,
                    model="deterministic_python",
                    purpose="grounded_event_answer",
                    started_at=self._now(),
                    ended_at=self._now(),
                    evidence_ids=[item.evidence_id for item in answer.evidence],
                    claim_links={},
                )
                self.store.finish_run(trace(), answer)
                return answer

            active_model = "thesis_impact_semantic_assessment"
            active_model_identity = getattr(
                self.semantic_judge, "model_name", type(self.semantic_judge).__name__
            )
            active_model_started = self._now()
            active_model_evidence = [item.evidence_id for item in event_items]
            semantic_result = await self.semantic_judge.assess(thesis, event_items)
            assessment = semantic_result.assessment
            semantic_item = semantic_result.evidence
            if semantic_item is None:
                raise ValueError("semantic judge did not produce persisted evidence")
            evidence.append(semantic_item)
            model_events.append(observed_model_event(self.semantic_judge,
                model=assessment.model,
                purpose="thesis_impact_semantic_assessment",
                started_at=active_model_started,
                ended_at=self._now(),
                evidence_ids=active_model_evidence,
                claim_links={"semantic_assessment": assessment.evidence_ids},
            ))
            active_model = None
            active_model_identity = None
            semantic_refs = [semantic_item.evidence_id]

            if self.comparison_semantic_judge is not None:
                comparison_purpose = "thesis_impact_semantic_assessment_comparison"
                comparison_identity = getattr(
                    self.comparison_semantic_judge,
                    "model_name",
                    type(self.comparison_semantic_judge).__name__,
                )
                comparison_started = self._now()
                try:
                    comparison_result = await self.comparison_semantic_judge.assess(
                        thesis, event_items
                    )
                    comparison_item = comparison_result.evidence
                    if comparison_item is None:
                        raise ValueError("comparison semantic judge did not produce evidence")
                    evidence.append(comparison_item)
                    semantic_refs.append(comparison_item.evidence_id)
                    model_events.append(observed_model_event(self.comparison_semantic_judge,
                        model=comparison_result.assessment.model,
                        purpose=comparison_purpose,
                        started_at=comparison_started,
                        ended_at=self._now(),
                        evidence_ids=active_model_evidence,
                        claim_links={
                            "comparison_semantic_assessment":
                                comparison_result.assessment.evidence_ids
                        },
                    ))
                except Exception as comparison_exc:
                    model_events.append(observed_model_event(self.comparison_semantic_judge,
                        model=comparison_identity,
                        purpose=comparison_purpose,
                        status="error",
                        started_at=comparison_started,
                        ended_at=self._now(),
                        evidence_ids=active_model_evidence,
                        claim_links={},
                        error=str(comparison_exc),
                    ))

            add_call(active_tool, active_args, started, semantic_refs)
            active_tool = None

            answer = self._event_answer(
                run_id, symbol, snapshot, thesis, thesis_item, event_items, semantic_item, assessment,
                self._deduplicate_evidence(evidence),
            )
            synthesis_started = self._now()
            synthesis = observed_model_event(self.llm,
                model="deterministic_python",
                purpose="grounded_event_answer",
                started_at=synthesis_started,
                ended_at=self._now(),
                evidence_ids=[item.evidence_id for item in answer.evidence],
                claim_links={claim.claim_id: claim.evidence_ids for claim in answer.claims},
            )
            self.store.finish_run(
                trace(answer.claims),
                answer,
                thesis_record=(thesis, thesis_item) if generated_thesis else None,
            )
            return answer
        except Exception as exc:
            if active_model is not None:
                model_events.append(observed_model_event(self.semantic_judge if active_model == "thesis_impact_semantic_assessment" else self.llm,
                    model=active_model_identity or self.llm.model_name,
                    purpose=active_model,
                    status="error",
                    started_at=active_model_started,
                    ended_at=self._now(),
                    evidence_ids=active_model_evidence,
                    claim_links={},
                    error=str(exc),
                ))
            if active_tool is not None:
                calls.append(ToolCall(
                    sequence=len(calls) + 1,
                    name=active_tool,
                    arguments=active_args,
                    status="error",
                    started_at=active_started,
                    ended_at=self._now(),
                    error=str(exc),
                ))
            self.store.fail_run(run_id, trace())
            if isinstance(exc, (ValueError, ProviderError, LLMError)):
                raise AgentRunError(run_id, str(exc)) from exc
            raise

    @staticmethod
    def _deduplicate_evidence(items: list[EvidenceItem]) -> list[EvidenceItem]:
        return list({item.evidence_id: item for item in items}.values())

    @staticmethod
    def _partition_initial_evidence(
        items: list[EvidenceItem],
    ) -> tuple[list[EvidenceItem], list[EvidenceItem]]:
        """Use earlier filings as baseline and retain all later company events."""
        if not items:
            return [], []
        filings = [item for item in items if item.evidence_type == "sec_filing"]
        news = [item for item in items if item.evidence_type == "news"]
        if filings and news:
            earliest_news = min(item.published_at for item in news)
            baseline = [item for item in filings if item.published_at < earliest_news]
            if baseline:
                boundary = max(item.published_at for item in baseline)
                events = sorted(
                    (item for item in items
                     if item not in baseline and item.published_at > boundary),
                    key=lambda item: (item.published_at, item.evidence_id),
                )
                if events:
                    return baseline, events
        event_time = max(item.published_at for item in items)
        return (
            [item for item in items if item.published_at < event_time],
            [item for item in items if item.published_at == event_time],
        )

    @staticmethod
    def _validate_event_evidence(
        items: list[EvidenceItem], symbol: str, cutoff: datetime
    ) -> None:
        if any(item.symbol != symbol for item in items):
            raise ValueError("event evidence symbol does not match query")
        if any(item.published_at is None or item.published_at > cutoff for item in items):
            raise ValueError("point-in-time leakage: event evidence exceeds requested cutoff")

    def _event_answer(
        self,
        run_id: str,
        symbol: str,
        snapshot: PortfolioSnapshot | None,
        thesis: ThesisProfile,
        thesis_item: EvidenceItem,
        event_items: list[EvidenceItem],
        semantic_item: EvidenceItem,
        assessment,
        evidence: list[EvidenceItem],
    ) -> AgentAnswer:
        event_ids = [item.evidence_id for item in event_items]
        event_names = "；".join(
            item.title or item.excerpt or item.evidence_id for item in event_items
        )
        impact_text = {
            "strengthens": "强化",
            "weakens": "削弱",
            "mixed": "影响混合",
            "no_material_change": "未构成实质改变",
            "insufficient": "证据不足",
        }[assessment.thesis_impact.value]
        support = [
            item.title or item.evidence_id for item in event_items
            if item.evidence_id in assessment.supporting_evidence_ids
        ]
        counter = [
            item.title or item.evidence_id for item in event_items
            if item.evidence_id in assessment.counter_evidence_ids
        ]
        support_text = "；".join(support) if support else "未识别到明确支持证据"
        counter_text = "；".join(counter) if counter else "未识别到独立反证"
        component_groups = {
            "core_driver": thesis.core_drivers,
            "key_assumption": thesis.key_assumptions,
            "execution_risk": thesis.execution_risks,
            "invalidation_condition": thesis.invalidation_conditions,
        }
        component_labels = {
            "core_driver": "核心驱动",
            "key_assumption": "关键假设",
            "execution_risk": "执行风险",
            "invalidation_condition": "失效条件",
        }
        component_impact_labels = {
            "strengthens": "强化",
            "weakens": "削弱",
            "mixed": "影响混合",
            "not_affected": "未受影响",
            "insufficient": "证据不足",
        }
        component_text = "；".join(
            f"{component_labels[item.component_type]}{item.component_index + 1}"
            f"“{component_groups[item.component_type][item.component_index].text}”"
            f"：{component_impact_labels[item.impact.value]}"
            for item in assessment.component_impacts
        )
        portfolio_text = (
            "BB 在当前组合中。" if snapshot and any(p.symbol == symbol for p in snapshot.positions)
            else "本次分析未使用 BB 的持仓数量或任何旧持仓标注。"
        )
        semantic_labels = {"none":"无", "low":"低", "medium":"中", "high":"高", "insufficient":"证据不足",
            "strengthens":"支持", "weakens":"削弱", "mixed":"影响混合", "neutral":"中性",
            "transient":"短期", "uncertain":"持续性待验证", "persistent":"持续"}
        answer_text = (
            f"相关事件：{event_names}。对 AI 草稿投资逻辑的判断：{impact_text}；"
            f"事件重要性为 {semantic_labels[assessment.event_materiality.value]}，方向为 {semantic_labels[assessment.direction.value]}，"
            f"持续性为 {semantic_labels[assessment.persistence.value]}，执行风险为 {semantic_labels[assessment.execution_risk.value]}。"
            f"逐项影响：{component_text}。"
            f"支持证据：{support_text}；反向/反证：{counter_text}。{portfolio_text}"
        )
        claims = [
            GroundedClaim(
                claim_id="C1",
                claim_type="fact",
                text=f"相关事件：{event_names}。",
                evidence_ids=event_ids,
            ),
            GroundedClaim(
                claim_id="C2",
                claim_type="judgment",
                text=(
                    f"对 AI 草稿投资逻辑的判断为{impact_text}；重要性、方向、持续性和执行风险"
                    f"均来自结构化语义评估。逐项影响：{component_text}。"
                ),
                evidence_ids=[
                    thesis_item.evidence_id,
                    semantic_item.evidence_id,
                    *assessment.evidence_ids,
                ],
            ),
        ]
        if snapshot and any(position.symbol == symbol for position in snapshot.positions):
            snapshot_item = next(
                item for item in evidence if item.evidence_type == "portfolio_snapshot"
            )
            claims.append(GroundedClaim(
                claim_id="C3",
                claim_type="fact",
                text="BB 在当前组合中。",
                evidence_ids=[snapshot_item.evidence_id],
            ))
        limitations = [
            "当前投资逻辑为 AI 草稿，尚未由用户确认。",
            *assessment.limitations,
        ]
        if not counter:
            limitations.append("未检索到独立反证不等于不存在反证。")
        return AgentAnswer(
            run_id=run_id,
            answer=answer_text,
            claims=claims,
            evidence=evidence,
            limitations=limitations,
        )
