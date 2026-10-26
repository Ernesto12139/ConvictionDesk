"""FastAPI surface for the deterministic first vertical slice."""

from pathlib import Path
import os
import asyncio
import logging
import traceback
from uuid import uuid4
from time import perf_counter
from collections.abc import Mapping

from fastapi import Body, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .agent import Agent, AgentRunError, MAX_EOD_AGE, MockLLMClient
from .analytics import fresh_observations
from .clock import Clock, system_utc_now
from .domain import AgentAnswer, HealthResponse, PortfolioSnapshot, QueryRequest, RunRecord
from .legacy import migrate_capital_desk_v1
from .llm import (LLMClient, UnconfiguredLLMClient, production_llm_from_environment)
from .persistence import Store
from .provider import (EODProvider, FixtureMarketProvider, MassiveGroupedEODProvider,
                       ProviderError, UnconfiguredEODProvider)
from .news import FinnhubNewsProvider, NewsProvider
from .sec import SECEDGARProvider
from .console import portfolio_overview, run_metrics
from .semantic import JevSemanticJudge
from .portfolio_entry import PortfolioEntry
from .portfolio_csv import (CSVConfirmRequest, CSVImportError, CSVTextRequest,
                            CSV_TEMPLATE, csv_source_hash, parse_portfolio_csv)
from .presentation import research_presentation
from .tdb import DecisionRecord, TDBError, TDBRequest, InvestmentContextRequest, content_hash
from .tdb_semantic import LLMDecisionJudge, build_semantic
from .quote import FinnhubQuoteProvider
from .cockpit import (CockpitRequest, CockpitResult, CockpitDecisionRequest, frame,
                      execute as execute_cockpit, project_decision, save_result)
from .fast_decision import FastDecisionJudge, run_fast
from .cockpit import save_fast_result


def llm_client_from_environment(environment: Mapping[str, str]) -> LLMClient:
    return production_llm_from_environment(environment)


# Rendered at the top of the public keyless page. The three data-nature classes are
# spelled out as visible text (not only in a tooltip or the README), because a reader
# cannot otherwise tell which numbers came from SEC, which were invented for the demo,
# and which came from a script instead of a model.
PUBLIC_IVS_BANNER = """
<section id="public-ivs-banner" class="public-ivs-banner" aria-labelledby="public-ivs-banner-title">
  <h2 id="public-ivs-banner-title">公开演示：从一份真实的 SEC 披露，走到一个可核查的研究判断</h2>
  <p class="public-ivs-gloss" lang="en">Public demo: one real public SEC filing receipt, a fictional portfolio and thesis, and a scripted research replay — keyless, offline, no model calls.</p>
  <ul class="public-ivs-classes">
    <li data-evidence-class="REAL_PUBLIC_SEC_METADATA"><strong>真实公开 SEC 申报元数据</strong><span>REAL_PUBLIC_SEC_METADATA · 冻结捕获，可打开 SEC 原文核对；只含元数据，不含申报正文。</span></li>
    <li data-evidence-class="SYNTHETIC"><strong>虚构持仓与 Thesis</strong><span>SYNTHETIC · 账户、数量与投资逻辑均为演示虚构，不是任何人的真实持仓或记录。</span></li>
    <li data-evidence-class="SCRIPTED"><strong>脚本回放（未运行 AI 模型）</strong><span>SCRIPTED · 0 次模型调用 · 无费用 · 不代表真实模型质量。</span></li>
  </ul>
  <p class="small muted">条目代码只用于提出核查问题，不证明价格或投资影响；UNKNOWN / UNDETERMINED 不会被显示成 HOLD；模拟不会下单，也不会修改任何真实组合。</p>
</section>"""


def create_app(db_path: str | Path = "data/market_agent.db",
               market_fixture: str | Path | None = None,
               market_provider: EODProvider | None = None,
               llm_client: LLMClient | None = None,
               clock: Clock | None = None,
               news_provider: NewsProvider | None = None,
               sec_provider: SECEDGARProvider | None = None,
               semantic_judge=None,
               comparison_semantic_judge=None,
               decision_judge=None, quote_provider=None, fast_judge=None,
               attention_screen=None, attention_deep=None, attention_fast_screen=None,
               research_reference=None, research_decision_component=None, sec_retriever=None,
               environment: Mapping[str, str] | None = None, model_profiles=None) -> FastAPI:
    environment = os.environ if environment is None else environment
    app_clock = clock or system_utc_now
    explicit_test_provider = market_fixture is not None or isinstance(market_provider, FixtureMarketProvider)
    store = Store(db_path)
    if market_provider is None:
        if market_fixture is not None:
            mode = "fixture"
        else:
            mode = environment.get("MARKET_PROVIDER") or (
                "massive" if environment.get("MASSIVE_API_KEY") else "unconfigured"
            )
        if mode == "massive":
            api_key = environment.get("MASSIVE_API_KEY", "")
            market_provider = (MassiveGroupedEODProvider(api_key, store, clock=app_clock)
                               if api_key else UnconfiguredEODProvider())
        elif mode == "fixture":
            market_provider = FixtureMarketProvider(
                market_fixture or "evals/fixtures/market_eod.json", clock=app_clock
            )
        elif mode == "unconfigured":
            market_provider = UnconfiguredEODProvider()
        else:
            raise ValueError("MARKET_PROVIDER must be fixture or massive")
    news_provider = news_provider or FinnhubNewsProvider(
        environment.get("FINNHUB_API_KEY", ""), clock=app_clock
    )
    if sec_retriever is None and environment.get('MIA_SEC_RETRIEVAL') == 'hybrid':
        from .hybrid_retrieval import HybridSECRetriever
        sec_retriever = HybridSECRetriever(store,model_folder=environment.get('MIA_EMBEDDING_MODEL_DIR'))
    sec_provider = sec_provider or SECEDGARProvider(
        store, environment.get("SEC_USER_AGENT", ""), clock=app_clock,retriever=sec_retriever
    )
    if research_reference is None and environment.get('MIA_RESEARCH_MCP') == 'readonly':
        from .longbridge_mcp import LongbridgeMCPClient,LongbridgeMCPQuoteProvider
        token = environment.get('LONGBRIDGE_MCP_ACCESS_TOKEN','')
        credential_path = environment.get('MIA_LONGBRIDGE_CREDENTIALS')
        if credential_path:
            from .local_credentials import load_credentials
            try:
                token = load_credentials(credential_path)['access_token']
            except (ValueError, RuntimeError, OSError):
                token = ''
                logging.getLogger(__name__).warning('Optional MCP protected credentials unavailable; market fallback retained')
        research_reference = LongbridgeMCPQuoteProvider(LongbridgeMCPClient(
            token),clock=app_clock)
    if quote_provider is None and not explicit_test_provider and environment.get("FINNHUB_API_KEY"):
        quote_provider = FinnhubQuoteProvider(environment["FINNHUB_API_KEY"], clock=app_clock)
    if comparison_semantic_judge is None and environment.get("JEV_API_KEY"):
        comparison_semantic_judge = JevSemanticJudge(
            environment["JEV_API_KEY"], clock=app_clock,
            model=environment.get("JEV_MODEL", "jev-latest"),
        )
    diagnostics_enabled = environment.get("CONSOLE_DIAGNOSTICS", "").lower() in {
        "1", "true", "yes", "on",
    }
    agent = Agent(
        store,
        market_provider,
        llm_client if llm_client is not None else (
            MockLLMClient() if explicit_test_provider else llm_client_from_environment(environment)),
        clock=app_clock,
        news=news_provider,
        sec=sec_provider,
        semantic_judge=semantic_judge,
        comparison_semantic_judge=comparison_semantic_judge,
    )
    app = FastAPI(title="ConvictionDesk", version="0.9.0")
    from .research_api import register_research
    if (model_profiles is None and not explicit_test_provider and llm_client is None
            and environment.get('LLM_PROVIDER','openai')=='openai'
            and environment.get('OPENAI_MODEL','gpt-6.1-sol')=='gpt-6.1-sol'):
        from .model_profiles import ModelProfiles
        model_profiles = ModelProfiles(environment)
    from .capital_reference import LocalCapitalReference
    # Local Owner runtime (D-088): Provider Hub, owned-data consent receipts and Jev
    # routing. Assembled before the research router; one Hub, one research API.
    runtime = None
    if environment.get('MIA_PROVIDER_HUB') == '1' and environment.get('MIA_RUNTIME_MODE') != 'PUBLIC_DEMO':
        from .runtime_bootstrap import configure_runtime
        runtime = configure_runtime(app, agent, environment)
    if runtime is None:
        # Task mutations are protected with or without the Hub (QA-CONVERGENCE-002 P2-1).
        from .local_api_security import install_task_origin_guard
        install_task_origin_guard(app)
    research_options = runtime.research_options() if runtime else {'model_profiles': model_profiles}
    register_research(app, agent,reference_provider=research_reference,decision_component=research_decision_component,
        local_capital_reference=LocalCapitalReference.from_environment(environment),
        strategy_policy=environment.get('MIA_RESEARCH_STRATEGY') or (
            'OPT_IN' if environment.get('MIA_RESEARCH_ADAPTIVE') == '1' else 'BASELINE_ONLY'),
        answer_contract=environment.get('MIA_RESEARCH_CONTRACT') or ('GROUNDED_MEMO' if runtime else 'LEGACY'),
        **research_options)
    if runtime:
        runtime.register_after_research(app)
    from .attention import AttentionService, GPTAttentionScreen, GPTAttentionDeep
    from .attention_api import register_attention
    register_attention(app,AttentionService(store,news_provider,
        attention_screen or GPTAttentionScreen(agent.llm,compact=True),attention_deep or GPTAttentionDeep(agent.llm),app_clock,
        fast_screen=attention_fast_screen,screen_batch_size=4 if attention_screen is None else 20,
        prefer_current=attention_screen is None))
    from .market_pulse import MarketPulse, RangeContext, register_pulse
    from .research_universe import ResearchStore
    # RangeContext builds its history client on first use, so startup still opens no
    # TLS context; without a Massive key it reports NOT_CONFIGURED instead of guessing.
    pulse = MarketPulse('' if explicit_test_provider else environment.get('FINNHUB_API_KEY', ''),
        ResearchStore(store).entries, clock=app_clock,
        range_source=RangeContext(
            '' if explicit_test_provider else environment.get('MASSIVE_API_KEY', ''),
            None if explicit_test_provider else store, clock=app_clock))
    register_pulse(app, pulse)
    from .ivs_today_api import FilingsService, register_filings

    def collect_filings(entries, *, as_of):
        from .sec_filings import project
        from .sec_thesis_delta import project as project_thesis
        projection = project(sec_provider, entries=entries, as_of=as_of)
        result = projection.model_dump(mode='json')
        result['thesis_deltas'] = project_thesis(projection, store).model_dump(mode='json')
        return result

    app.state.ivs_scan_observation = None
    filings_service = FilingsService(entries=ResearchStore(store).entries,
        clock=app_clock, collect=collect_filings,
        observer=lambda row: setattr(app.state, 'ivs_scan_observation', row))
    register_filings(app, filings_service)
    app.state.research_service.filing_source = filings_service
    from .portfolio_preview import register_portfolio_preview
    register_portfolio_preview(app, store, pulse, app_clock)
    from .route_preview import register_route_preview
    register_route_preview(app, ResearchStore(store).entries, app_clock,
        model_profiles=runtime.profiles if runtime else None, jev_runtime=runtime.jev if runtime else None)
    jobs = {}
    app.state.decision_jobs = jobs
    def decision_error_detail(exc):
        detail = {"code": exc.code, "run_id": exc.run_id}
        if exc.run_id and exc.code in {"MARKET_DATA_UNAVAILABLE", "MARKET_DATA_INVALID"}:
            failed = store.get_run(exc.run_id)
            call = next((call for call in reversed(failed.trace.tool_calls)
                         if call.name == "market.get_eod" and call.status == "error"), None) if failed and failed.trace else None
            if call and call.error in {"configuration", "rate_limit", "retryable", "transport",
                                       "timeout", "provider", "invalid_input", "invalid_data"}:
                detail["provider_error"] = call.error
        if exc.run_id and exc.code in {"DECISION_JUDGE_FAILED", "DECISION_JUDGE_UNAVAILABLE"}:
            failed = store.get_run(exc.run_id)
            trace = failed.trace if failed else None
            call = next((c for c in reversed(trace.tool_calls)
                if c.name in {"decision.judge", "decision.fast"} and c.status == "error"), None) if trace else None
            # Expose only known typed causes, never exception text or inputs.
            if call and call.error in {"timeout", "transport", "authentication", "rate_limit",
                "provider_unavailable", "provider", "configuration", "malformed_json",
                "schema_validation", "incomplete_response", "invalid_evidence_reference",
                "unsupported_numeric_fact", "price_context_ignored", "unsupported_thesis_claim",
                "refusal", "empty_content", "context_budget_exceeded"}:
                detail["judge_error"] = call.error
            event = trace.model_events[-1] if trace and trace.model_events else None
            attempt = event.transport_attempts[-1] if event and event.transport_attempts else {}
            if attempt.get("timeout_phase") in {"connect", "read", "write", "pool"}:
                detail["timeout_phase"] = attempt["timeout_phase"]
        return detail
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.middleware("http")
    async def browser_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic's default response includes input values, possibly private imports.
        return JSONResponse(status_code=422, content={"detail": "INVALID_REQUEST"})

    @app.exception_handler(TDBError)
    async def decision_failure(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"detail": decision_error_detail(exc)})

    @app.get("/", include_in_schema=False)
    def console_page():
        if getattr(app.state, "public_ivs", None):
            # The public keyless slice composes the same console, but the page has to
            # say what is real, what is invented and what is scripted before the
            # reader clicks anything. `public_ivs` is set by
            # `market_agent.public_ivs.build_app`; without this branch the public build
            # silently rendered the Owner console with no labels at all.
            html = (static / "index.html").read_text(encoding="utf-8")
            html = html.replace(
                'data-runtime-mode="OWNER"',
                'data-runtime-mode="OWNER" data-ivs-preview="keyless" data-public-ivs="1"', 1)
            return HTMLResponse(html.replace('<main>', '<main>' + PUBLIC_IVS_BANNER, 1))
        if getattr(app.state, "ivs_preview", None):
            html = (static / "index.html").read_text(encoding="utf-8")
            preview_kind = 'keyless' if app.state.ivs_preview['keyless'] else 'scripted'
            html = html.replace('data-runtime-mode="OWNER"', f'data-runtime-mode="OWNER" data-ivs-preview="{preview_kind}"', 1)
            notice = '<p id="ivs-preview-notice" class="status-line warning" role="note">IVS 隔离预览：真实 SEC 元数据的冻结捕获；账户与 Thesis 均为合成；研究输出为本地脚本演示。无模型费用、无自动外部调用，不代表真实模型质量。</p>'
            return HTMLResponse(html.replace('<main>', '<main>' + notice, 1))
        if getattr(app.state, "public_demo", None):
            html = (static / "index.html").read_text(encoding="utf-8")
            html = html.replace('data-runtime-mode="OWNER"', 'data-runtime-mode="PUBLIC_DEMO"')
            return HTMLResponse(html.replace('role="note" hidden>', 'role="note">'))
        return FileResponse(static / "index.html")

    @app.get("/api/console/status")
    def console_status():
        # Deliberately allowlisted: no environment mapping, keys, URLs or headers.
        status = {
            "provider_hub_enabled": runtime is not None,
            "market_mode": (
                "fixture" if isinstance(market_provider, FixtureMarketProvider) else
                "unconfigured" if isinstance(market_provider, UnconfiguredEODProvider) else "provider"
            ),
            "market_provider": getattr(market_provider, "name", "custom"),
            "synthesis_mode": ("mock" if isinstance(agent.llm, MockLLMClient) else
                               "unconfigured" if isinstance(agent.llm, UnconfiguredLLMClient) else "model"),
            "model": agent.llm.model_name,
            "comparison_mode": (
                "off" if comparison_semantic_judge is None else
                "jev" if isinstance(comparison_semantic_judge, JevSemanticJudge) else "configured"
            ),
            "comparison_model": getattr(comparison_semantic_judge, "model_name", None),
            "diagnostics_enabled": diagnostics_enabled,
        }
        identity = getattr(app.state, "local_acceptance", None)
        if identity:
            status["local_acceptance"] = {key: identity[key] for key in
                ("head", "source_sha256", "pid", "status")}
        public = getattr(app.state, "public_ivs", None)
        if public:
            # A public build must never name a production model it did not call.
            # Reported from the manifest so the label and the served behaviour agree.
            model = public.get("model") or {}
            status["synthesis_mode"] = "scripted"
            status["model"] = model.get("name") or "SCRIPTED_ORACLE_NOT_A_REAL_MODEL"
            status["public_ivs"] = {
                "keyless": bool(public.get("keyless")),
                "paid_model_calls": public.get("paid_model_calls", 0),
                "live_network": bool(public.get("live_network")),
                "as_of": public.get("as_of"),
                "evidence_classes": ["REAL_PUBLIC_SEC_METADATA", "SYNTHETIC", "SCRIPTED"],
            }
        elif getattr(app.state, "public_demo", None):
            status["public_demo"] = app.state.public_demo
            if status["synthesis_mode"] == "unconfigured":
                status["model"] = None
        return status

    @app.get("/api/console/portfolio-overview")
    def console_portfolio_overview():
        snapshot = store.get_snapshot()
        if snapshot is None:
            raise HTTPException(status_code=404, detail="No portfolio snapshot")
        cutoff = app_clock()
        observations = []
        provider_error = None
        try:
            result = market_provider.retrieve_eod(
                sorted({position.symbol for position in snapshot.positions}), cutoff
            )
            observations, _ = fresh_observations(result.observations, cutoff, MAX_EOD_AGE)
        except ProviderError as exc:
            provider_error = exc.code
        return portfolio_overview(snapshot, observations, cutoff=cutoff,
                                  provider_error=provider_error)

    @app.get("/api/console/runs/{run_id}/metrics")
    def console_metrics(run_id: str):
        record = store.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return run_metrics(record)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    @app.post("/api/portfolio", response_model=PortfolioSnapshot)
    def save_portfolio(payload: PortfolioEntry) -> PortfolioSnapshot:
        try:
            snapshot = payload.snapshot(app_clock())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="INVALID_PORTFOLIO") from exc
        try:
            store.add_snapshot_if_current(snapshot, payload.expected_snapshot_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="PORTFOLIO_CHANGED") from exc
        return snapshot

    @app.get("/api/portfolio/csv/template")
    def csv_template():
        return PlainTextResponse("\ufeff" + CSV_TEMPLATE, media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": "attachment; filename=portfolio-template.csv"})

    @app.post("/api/portfolio/csv/preview")
    def preview_csv(payload: CSVTextRequest):
        current = store.get_snapshot()
        try:
            entry = parse_portfolio_csv(payload.text, current.snapshot_id if current else None)
        except CSVImportError as exc:
            raise HTTPException(status_code=422, detail={"code": exc.code, "row": exc.row}) from exc
        return {"accounts": [item.model_dump(mode="json") for item in entry.accounts],
                "positions": [item.model_dump(mode="json") for item in entry.positions],
                "instrument_overrides": {key: value.model_dump(mode="json")
                                         for key, value in entry.instrument_overrides.items()},
                "expected_snapshot_id": entry.expected_snapshot_id,
                "source_hash": csv_source_hash(payload.text)}

    @app.post("/api/portfolio/csv/import", response_model=PortfolioSnapshot)
    def import_csv(payload: CSVConfirmRequest):
        if csv_source_hash(payload.text) != payload.source_hash:
            raise HTTPException(status_code=422, detail={"code": "PREVIEW_CHANGED"})
        try:
            entry = parse_portfolio_csv(payload.text, payload.expected_snapshot_id)
            snapshot = entry.snapshot(app_clock(), source_kind="csv_import",
                                      source_hash=payload.source_hash)
        except CSVImportError as exc:
            raise HTTPException(status_code=422, detail={"code": exc.code, "row": exc.row}) from exc
        try:
            store.add_snapshot_if_current(snapshot, payload.expected_snapshot_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail={"code": "PORTFOLIO_CHANGED"}) from exc
        return snapshot

    @app.get("/api/console/runs/{run_id}/presentation")
    def console_presentation(run_id: str):
        record = store.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return research_presentation(record)

    @app.post("/api/portfolio/import/capital-desk-v1", response_model=PortfolioSnapshot)
    def import_capital_desk_v1(payload: dict = Body(...)) -> PortfolioSnapshot:
        try:
            snapshot = migrate_capital_desk_v1(payload, imported_at=app_clock())
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise HTTPException(status_code=422, detail="INVALID_CAPITAL_DESK_V1") from exc
        store.add_snapshot(snapshot)
        return snapshot

    @app.get("/api/portfolio/current", response_model=PortfolioSnapshot)
    def current_portfolio() -> PortfolioSnapshot:
        snapshot = store.get_snapshot()
        if snapshot is None:
            raise HTTPException(status_code=404, detail="No portfolio snapshot")
        return snapshot

    @app.post("/api/decisions/tdb-foundation", response_model=DecisionRecord)
    def create_tdb_foundation(request: TDBRequest) -> DecisionRecord:
        try:
            return agent.create_tdb_foundation(request)
        except TDBError as exc:
            raise HTTPException(status_code=exc.status_code,
                                detail=decision_error_detail(exc)) from exc

    @app.post("/api/decisions/tdb-semantic", response_model=DecisionRecord)
    async def create_tdb_semantic(request: TDBRequest) -> DecisionRecord:
        try:
            return await build_semantic(request, store, market_provider, news_provider,
                                        sec_provider, decision_judge or LLMDecisionJudge(agent.llm),
                                        app_clock, agent.thesis_generator, quote_provider=quote_provider)
        except TDBError as exc:
            raise HTTPException(status_code=exc.status_code,
                                detail=decision_error_detail(exc)) from exc

    def recovery_packet(run_id):
        failed = store.get_run(run_id)
        if not failed or failed.status != "error" or not failed.trace:
            raise HTTPException(409, detail={"code": "CHECKPOINT_NOT_RECOVERABLE"})
        call = next((c for c in reversed(failed.trace.tool_calls) if c.name in {"decision.judge", "decision.fast"} and c.status == "error"), None)
        checkpoint = call.output_refs[-1] if call and call.output_refs else run_id
        packet = store.get_decision_checkpoint(checkpoint)
        if not packet:
            raise HTTPException(409, detail={"code": "CHECKPOINT_NOT_RECOVERABLE"})
        return checkpoint, packet

    def start_job(request, checkpoint=None, *, cockpit_framing=None, deep_cockpit=None, recover_fast=False, recovery_run_id=None):
        # In-process polling is sufficient for the local single-server console. Frozen
        # model inputs survive restart; running jobs do not pretend to survive it.
        for key in list(jobs):
            if len(jobs) < 64:
                break
            if jobs[key]["status"] != "running":
                del jobs[key]
        if len(jobs) >= 64:
            raise HTTPException(429, detail={"code": "TOO_MANY_RUNNING_DECISIONS"})
        job_id = str(uuid4())
        job = {"status": "running", "stage": "queued", "completed_stages": [],
               "context": None, "evidence_count": None, "decision_id": None, "error": None}
        if cockpit_framing:
            job.update(framing=cockpit_framing.model_dump(mode="json"), cockpit_id=None)
        jobs[job_id] = job
        started_timer = perf_counter()
        job["timings_ms"] = {}
        def progress(stage, values):
            previous = job["stage"]
            if previous != stage and previous not in {"queued", "error", "complete"}:
                job["completed_stages"].append(previous)
            job["stage"] = stage
            if stage == "context":
                job["timings_ms"].setdefault("first_deterministic_context_ms", round((perf_counter()-started_timer)*1000))
            for key in ("context", "evidence_count", "decision_id", "run_id", "checkpoint_id", "framing", "investigation"):
                if key in values:
                    job[key] = values[key]
        async def execute():
            try:
                if recover_fast:
                    brief, run_id = await run_fast(store, checkpoint, fast_judge or FastDecisionJudge(agent.llm), app_clock, progress=progress)
                    result = save_fast_result(store, cockpit_framing, brief, app_clock(), checkpoint, run_id)
                    job.update(status="success", stage="complete", cockpit_id=result.cockpit_id, run_id=run_id,
                        framing=result.framing.model_dump(mode="json"))
                    job["timings_ms"]["fast_decision_ready_ms"] = round((perf_counter()-started_timer)*1000)
                    return
                if cockpit_framing and checkpoint is None:
                    result = await execute_cockpit(cockpit_framing, agent,
                        decision_judge or LLMDecisionJudge(agent.llm), progress=progress, quote_provider=quote_provider,
                        fast_judge=(fast_judge or FastDecisionJudge(agent.llm)) if fast_judge is not False else None)
                    job.update(status="success", stage="complete", cockpit_id=result.cockpit_id,
                        run_id=result.run_id, decision_id=result.decision_id,
                        framing=result.framing.model_dump(mode="json"))
                    job["timings_ms"]["fast_decision_ready_ms" if result.version == 2 else "deep_decision_ready_ms"] = round((perf_counter()-started_timer)*1000)
                    return
                from .tdb_workflow import build
                record = await build(request, store, market_provider, news_provider,
                    sec_provider, decision_judge or LLMDecisionJudge(agent.llm), app_clock,
                    checkpoint_id=checkpoint, progress=progress, quote_provider=quote_provider,
                    frozen_input=deep_cockpit is not None, recovery_run_id=recovery_run_id)
                job.update(status="success", decision_id=record.decision_id)
                job["timings_ms"]["deep_decision_ready_ms"] = round((perf_counter()-started_timer)*1000)
                if cockpit_framing:
                    result = save_result(store, cockpit_framing, project_decision(record), app_clock(),
                        decision_id=record.decision_id, run_id=record.run_id)
                    job["cockpit_id"] = result.cockpit_id
            except TDBError as exc:
                detail = decision_error_detail(exc)
                job.update(status="error", error=detail)
                if exc.code in {"DECISION_JUDGE_FAILED", "DECISION_JUDGE_UNAVAILABLE"}:
                    logging.getLogger("market_agent.decisions").warning(
                        "decision_job_failed job_id=%s run_id=%s code=%s cause=%s timeout_phase=%s",
                        job_id, exc.run_id, exc.code, detail.get("judge_error", "unknown"),
                        detail.get("timeout_phase", "unknown"))
            except AgentRunError as exc:
                job.update(status="error", error={"code":"COCKPIT_RESEARCH_FAILED", "run_id":exc.run_id})
            except Exception as exc:
                # No private provider payload or input may enter the browser error.
                frames = [f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                          for frame in traceback.extract_tb(exc.__traceback__)
                          if Path(frame.filename).parent.name == "market_agent"]
                logging.getLogger("market_agent.decisions").error(
                    "decision_job_failed job_id=%s stage=%s exception_type=%s frames=%s",
                    job_id, job["stage"], type(exc).__name__, ",".join(frames))
                job.update(status="error", error={"code": "DECISION_INTERNAL_ERROR", "job_id": job_id})
        job["task"] = asyncio.create_task(execute())
        return {"job_id": job_id, **({"framing":job["framing"]} if cockpit_framing else {})}

    @app.post("/api/cockpit/jobs", status_code=202)
    async def create_cockpit_job(request: CockpitRequest):
        timer = perf_counter()
        framing = frame(request, store.get_snapshot_known_at(app_clock()))
        result = start_job(request, cockpit_framing=framing)
        jobs[result["job_id"]]["timings_ms"]["framing_ack_ms"] = round((perf_counter()-timer)*1000)
        return result

    @app.get("/api/cockpit/{cockpit_id}/context")
    def read_cockpit_context(cockpit_id: str):
        result = read_cockpit(cockpit_id)
        if result.version != 2:
            raise HTTPException(409, detail={"code":"LEGACY_COCKPIT_HAS_DEEP_RECORD"})
        packet = store.get_decision_checkpoint(result.checkpoint_id)
        return {"checkpoint_id":result.checkpoint_id, "checkpoint_hash":content_hash(packet),
            "investment_context":packet["facts"]["investment_context"], "investigation":packet["facts"]["investigation"],
            "evidence":packet["selected"], "evidence_cutoff":packet["cutoff"]}

    @app.post("/api/cockpit/{cockpit_id}/deep", status_code=202)
    async def create_cockpit_deep(cockpit_id: str):
        result = read_cockpit(cockpit_id)
        if result.version != 2:
            raise HTTPException(409, detail={"code":"LEGACY_COCKPIT_HAS_DEEP_RECORD"})
        packet = store.get_decision_checkpoint(result.checkpoint_id)
        request = CockpitDecisionRequest.model_validate(packet["request"])
        return start_job(request, result.checkpoint_id, deep_cockpit=cockpit_id)

    @app.get("/api/cockpit/{cockpit_id}", response_model=CockpitResult)
    def read_cockpit(cockpit_id: str):
        try:
            result = store.get_cockpit_result(cockpit_id)
        except TDBError as exc:
            raise HTTPException(exc.status_code, detail={"code":exc.code}) from None
        if result is None:
            raise HTTPException(404, detail={"code":"COCKPIT_NOT_FOUND"})
        return result

    @app.post("/api/decisions/jobs", status_code=202)
    async def create_decision_job(request: TDBRequest):
        return start_job(request)

    @app.post("/api/decisions/context/jobs", status_code=202)
    async def create_context_job(request: InvestmentContextRequest):
        return start_job(request)

    @app.get("/api/decisions/context/options/{symbol}")
    def context_options(symbol: str):
        try:
            normalized = TDBRequest.valid_symbol(symbol)
        except ValueError:
            raise HTTPException(422,detail={"code":"INVALID_SYMBOL"}) from None
        thesis = store.get_thesis(normalized,app_clock())
        return {"thesis_version_id":thesis.thesis_version_id if thesis else None,
                "invalidation_conditions":[],
                "adverse_condition_status":"UNSPECIFIED",
                "requires_independent_adverse_premise":True}

    @app.get("/api/decisions/jobs/{job_id}")
    def decision_job(job_id: str):
        if job_id not in jobs:
            raise HTTPException(404, detail={"code": "DECISION_JOB_NOT_FOUND"})
        return {key: value for key, value in jobs[job_id].items() if key != "task"}

    @app.post("/api/decisions/recover/{run_id}", status_code=202)
    async def recover_decision_job(run_id: str):
        checkpoint, packet = recovery_packet(run_id)
        request_model = CockpitDecisionRequest if "decision_framing" in packet["request"] else (
            InvestmentContextRequest if "intent" in packet["request"] else TDBRequest)
        request = request_model.model_validate(packet["request"])
        failed = store.get_run(run_id)
        is_fast = any(c.name == "decision.fast" and c.status == "error" for c in failed.trace.tool_calls)
        return start_job(request, checkpoint, cockpit_framing=getattr(request, "decision_framing", None),
            recover_fast=is_fast, recovery_run_id=None if is_fast else run_id)

    @app.get("/api/decisions/{decision_id}", response_model=DecisionRecord)
    def read_decision(decision_id: str) -> DecisionRecord:
        record = store.get_decision(decision_id)
        if record is None:
            raise HTTPException(status_code=404, detail={"code": "DECISION_NOT_FOUND"})
        return record

    @app.get("/api/decisions/{decision_id}/evidence")
    def read_decision_evidence(decision_id: str):
        record = store.get_decision(decision_id)
        if record is None:
            raise HTTPException(status_code=404, detail={"code": "DECISION_NOT_FOUND"})
        return store.get_run_evidence(record.run_id)

    @app.post("/api/query", response_model=AgentAnswer)
    async def query(request: QueryRequest) -> AgentAnswer:
        try:
            return await agent.query(request)
        except AgentRunError as exc:
            if exc.provider_code:
                invalid_input = exc.provider_code == "invalid_input"
                raise HTTPException(
                    status_code=422 if invalid_input else 503,
                    detail={"run_id": exc.run_id,
                            "error": "MARKET_DATA_INVALID_INPUT" if invalid_input
                            else "MARKET_DATA_UNAVAILABLE",
                            "code": exc.provider_code},
                ) from exc
            raise HTTPException(status_code=409, detail={"run_id": exc.run_id, "error": str(exc)}) from exc

    @app.get("/api/runs/{run_id}", response_model=RunRecord)
    def run(run_id: str) -> RunRecord:
        record = store.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return record

    return app


def app_from_environment(environment):
    mode = environment.get('MIA_RUNTIME_MODE', 'OWNER')
    if mode == 'PUBLIC_DEMO':
        from .public_demo import create_public_demo
        return create_public_demo(create_app, environment)
    if mode != 'OWNER':
        raise ValueError('MIA_RUNTIME_MODE must be OWNER or PUBLIC_DEMO')
    profiles = None
    if (environment.get('LLM_PROVIDER','openai')=='openai'
            and (environment.get('OPENAI_MODEL','').strip() or 'gpt-6.1-sol')=='gpt-6.1-sol'):
        from .model_profiles import ModelProfiles
        profiles = ModelProfiles(environment)
    return create_app(db_path=environment.get('MIA_DB_PATH', 'data/market_agent.db'),
        llm_client=llm_client_from_environment(environment), environment=environment, model_profiles=profiles)


app = app_from_environment(os.environ)
