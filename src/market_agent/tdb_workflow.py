"""Bounded research and a durable model-stage checkpoint for owner decisions."""
import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

from .clock import system_utc_now
from .domain import (EvidenceItem, ToolCall, NewsSearchToolArgs, SECSearchToolArgs,
                     SemanticToolArgs, PreviousDecisionToolArgs, RoutePlan, RouteIntent,
                     RunTrace, SynthesisEvent, utc)
from .provider import ProviderError
from .llm import LLMError
from .tdb import (TDBRequest, TDBError, build_foundation, DecisionRecord, EvidenceManifest,
                  EvidenceReference, content_hash, price_difference_percentage)
from .tdb_contract import (JudgeResult, ThesisDelta, DecisionAssessment, SemanticArtifact,
                           EvidenceGap, ResearchCompletion, normalize_support)
from .decision_context import context_evidence
from .quote import current_reference
from .thesis import ThesisProfile
from .tdb import InvestmentContextRequest
from .investment_context import (InvestmentContextPacket, CloseHistory, market_structure,
    build_packet, reported_facts, validate_conditional, ConditionalAssessment, ConditionedStatement, conditional_statements)
from .analytics import fresh_observations
from decimal import Decimal

FORMS = ["10-K", "10-Q", "8-K", "20-F", "40-F", "6-K"]
BUSINESS_QUERY = "revenue operating income cash flow guidance liquidity risk earnings litigation regulatory"
TARGET_QUERY = "revenue profitability cash liquidity outlook regulatory execution"
ALLOWED_TOOLS = {"portfolio.get_snapshot", "market.get_eod", "market.get_reference", "thesis.get",
    "decision.get_previous_review", "news.search", "sec.search", "sec.fundamentals",
    "sec.targeted_search", "evidence.get_persisted_events", "decision.resume_checkpoint", "decision.judge"}
ALLOWED_TOOLS |= {"market.get_portfolio_eod", "market.get_completed_history"}
ALLOWED_TOOLS |= {"market.get_benchmark_history", "premise.verify", "investigation.plan", "context.project"}
ALLOWED_TOOLS |= {"decision.align_context_cutoff"}


def _context_at_cutoff(raw, cutoff):
    """Align metadata at final freeze without changing any financial inputs."""
    context = InvestmentContextPacket.model_validate(raw)
    if context.cutoff == cutoff:
        return context
    if context.cutoff > cutoff:
        raise ValueError("context cutoff cannot move backwards")
    payload = context.model_dump(mode="json", exclude={"packet_hash"})
    payload["cutoff"] = utc(cutoff).isoformat().replace("+00:00", "Z")
    return InvestmentContextPacket(**payload, packet_hash=content_hash(payload))


def evidence_gaps(items):
    """Conservative preflight, not a claim that counts prove semantic sufficiency."""
    gaps = []
    public = [i for i in items if i.evidence_type in {"news", "sec_filing"} and (i.excerpt or "").strip()]
    if not public:
        gaps.append(EvidenceGap(dimension="COMPANY_SPECIFIC", description="缺少可检查的公司具体材料。",
                                query=TARGET_QUERY))
    if not any(i.evidence_type == "sec_filing" for i in public):
        gaps.append(EvidenceGap(dimension="BUSINESS_PERFORMANCE", description="缺少经营表现与公司披露，需要核查盈利、现金流和执行风险。",
                                query=TARGET_QUERY))
    return tuple(gaps)


def unresolved_provider_errors(calls):
    last = {c.name: c for c in calls if c.name in {"news.search", "sec.search", "sec.fundamentals", "sec.targeted_search"}}
    return tuple(f"{name}:{c.error}" for name, c in last.items() if c.status == "error")


async def build(request, store, market, news, sec, judge, clock=system_utc_now,
                *, checkpoint_id=None, progress=None, quote_provider=None, prepare_only=False,
                frozen_input=False, recovery_run_id=None):
    from .tdb_semantic import _price_relation, _validate_judgment, _price_tokens, depth_statements
    recovering = checkpoint_id is not None
    run_id = str(uuid4())
    is_idc = isinstance(request, InvestmentContextRequest)
    hypothetical = is_idc and request.intent == "HYPOTHETICAL"
    contract = "tdb-decision-judge-v8-idc" if is_idc else "tdb-decision-judge-v7"

    def emit(stage, **values):
        if progress:
            progress(stage, values)

    def trace(run_id, calls, selected, events, links=None):
        if {c.name for c in calls} - ALLOWED_TOOLS:
            raise ValueError("TDB tool outside allowlist")
        framing = getattr(request, "decision_framing", None)
        return RunTrace(run_id=run_id, query=framing.query if framing else f"TDB-2 semantic: {request.symbol}",
            requested_as_of=request.as_of,
            route_plan=RoutePlan(intent=RouteIntent.TRIGGERED_DECISION_BRIEF, symbols=[request.symbol],
                requested_as_of=request.as_of, required_tools=list(dict.fromkeys(c.name for c in calls)),
                needs_semantic_judgment=True), tool_calls=calls, synthesis=None,
            model_events=events, evidence_ids=[i.evidence_id for i in selected], final_claim_links=links or {})

    def eligible(items):
        if any(i.symbol != request.symbol for i in items):
            store.record_failed_decision_run(trace(run_id, calls, selected, []), selected)
            raise TDBError("EVIDENCE_SYMBOL_MISMATCH", 409, run_id)
        seen = set()
        result = []
        for i in sorted(items, key=lambda i: i.published_at or datetime.min.replace(tzinfo=cutoff.tzinfo), reverse=True):
            if (i.published_at is None or i.published_at > cutoff or i.retrieved_at > cutoff
                or i.content_hash in seen):
                continue
            if is_idc and (i.structured_data or {}).get("metrics"):
                from .investment_context import ReportedFact
                try:
                    for m in i.structured_data["metrics"]:
                        if not isinstance(m,dict):
                            raise ValueError('invalid financial metric shape')
                        # Original v0.9 summaries mixed shares with USD facts.
                        # Keep those bytes intact and skip only that separate
                        # quantity type; malformed monetary facts still reject
                        # the source. New summaries use share_facts instead.
                        if m.get('metric')=='shares_outstanding' and m.get('unit')=='shares':
                            continue
                        fact = ReportedFact(metric=m["metric"],value=m["value"],unit=m["unit"],
                            start=m.get("start"),end=m["end"],accession=m["accession_number"],
                            accepted_at=m["available_at"],evidence_id=i.evidence_id)
                        if fact.accepted_at>cutoff or fact.end>fact.accepted_at.date() or (fact.start and fact.start>fact.end):
                            raise ValueError("financial fact unavailable at cutoff")
                except (ValueError,KeyError,TypeError):
                    # Fail closed before either model projection or frozen packet.
                    continue
            seen.add(i.content_hash)
            result.append(i)
        return result[:20]


    if checkpoint_id:
        packet = store.get_decision_checkpoint(checkpoint_id)
        if not packet or packet["request"] != request.model_dump(mode="json"):
            raise TDBError("CHECKPOINT_NOT_FOUND", 404)
        old_run = store.get_run(recovery_run_id or packet.get("run_id", checkpoint_id))
        if recovery_run_id and (not old_run or not old_run.trace or not any(
            c.name=="decision.judge" and c.status=="error" and c.output_refs and c.output_refs[-1]==checkpoint_id
            for c in old_run.trace.tool_calls)):
            raise TDBError("CHECKPOINT_NOT_RECOVERABLE", 409)
        if not frozen_input and (not old_run or old_run.status != "error" or not any(c.name == "decision.judge" and c.status == "error"
                                                               for c in old_run.trace.tool_calls)):
            raise TDBError("CHECKPOINT_NOT_RECOVERABLE", 409)
        foundation = store.get_decision(packet["foundation_id"])
        emit("context", context=packet["facts"]["decision_context"], resumed=True)
    else:
        emit("portfolio")
        foundation = await asyncio.to_thread(build_foundation, request, store, market, clock)
        framing = getattr(request, "decision_framing", None)
        if framing and foundation.portfolio_snapshot_id != framing.snapshot_id:
            raise TDBError("COCKPIT_PORTFOLIO_CHANGED", 409)
        calls = list(store.get_run(foundation.run_id).trace.tool_calls)
        selected = store.get_run_evidence(foundation.run_id)
        started = utc(clock())
        cutoff = request.as_of or started
        historical = request.as_of is not None
        prior = store.get_previous_semantic_decision(request.symbol, cutoff)
        thesis = foundation.manifest.thesis
        calls[3] = calls[3].model_copy(update={"arguments": PreviousDecisionToolArgs(symbol=request.symbol, before=cutoff),
                                               "output_refs": [prior.decision_id] if prior else []})
        if not prepare_only and getattr(judge, "configuration_error", None):
            event = SynthesisEvent(model=judge.model_name, purpose=contract, status="error",
                started_at=started, ended_at=utc(clock()), evidence_ids=[], claim_links={}, error="configuration")
            store.record_failed_decision_run(trace(run_id, calls, selected, [event]), selected)
            raise TDBError("DECISION_JUDGE_UNAVAILABLE", 503, run_id)

        async def retrieve(name, args, operation, *, optional=True):
            for attempt in range(2):
                if getattr(request, "decision_framing", None) and sum(c.name in {
                    "news.search", "sec.search", "sec.fundamentals", "sec.targeted_search"} for c in calls) >= 8:
                    raise TDBError("INVESTIGATION_BUDGET_EXCEEDED", 409, run_id)
                began = utc(clock())
                try:
                    result = await asyncio.to_thread(operation)
                    refs = [i.evidence_id for i in getattr(result, "evidence", [])]
                    calls.append(ToolCall(sequence=len(calls)+1, name=name, arguments=args, status="success",
                        started_at=began, ended_at=utc(clock()), output_refs=refs))
                    return result
                except ProviderError as exc:
                    calls.append(ToolCall(sequence=len(calls)+1, name=name, arguments=args, status="error",
                        started_at=began, ended_at=utc(clock()), error=exc.code))
                    if attempt == 0 and exc.code in {"retryable", "transport", "timeout", "rate_limit"}:
                        continue
                    if not optional or exc.code in {"invalid_data", "invalid_input"}:
                        store.record_failed_decision_run(trace(run_id, calls, selected, []), selected)
                        raise TDBError(name.split('.')[0].upper()+"_UNAVAILABLE", 503, run_id) from None
                    return None

        ref, ref_item, ref_status = None, None, "HISTORICAL_DISABLED" if historical else "UNAVAILABLE"
        if not historical:
            began = utc(clock())
            try:
                ref, ref_item, ref_status = await asyncio.to_thread(current_reference, market, quote_provider, request.symbol, utc(clock()))
            except ProviderError as exc:
                ref_status = exc.code
            calls.append(ToolCall(sequence=len(calls)+1, name="market.get_reference", arguments=None,
                status="success" if ref_status in {"AVAILABLE", "UNAVAILABLE", "NOT_ENTITLED", "NO_TIMESTAMPED_PRICE"} else "error",
                started_at=began, ended_at=utc(clock()), output_refs=[ref_item.evidence_id] if ref_item else [],
                error=None if ref_item else ref_status))
            if ref_item:
                selected.append(ref_item)
        snapshot = store.get_snapshot_known_at(foundation.analysis_cutoff)
        context, context_item = context_evidence(request, foundation, snapshot, ref, ref_status, now=utc(clock()))
        selected.append(context_item)
        structure = market_structure(None, request.symbol, request.decision_price, cutoff,
            unknown_reason="HISTORICAL_VINTAGE_UNAVAILABLE" if historical else None)
        nav_numerator = context.scenario_value_usd
        nav_denominator = nav_weight = None
        missing_symbols = sorted({p.symbol for p in snapshot.positions if p.symbol != request.symbol})
        valuation_evidence_ids = []
        history = None
        history_error = None
        if is_idc:
            # Only other symbols require EOD prices; target uses explicit USER_INPUT.
            # A partial denominator never produces a complete NAV percentage.
            other_prices = {}
            if missing_symbols:
                began = utc(clock())
                try:
                    result = await asyncio.to_thread(market.retrieve_eod, missing_symbols, cutoff)
                    from .tdb import _observation_matches_evidence, _pit_eligible
                    by_market = {i.evidence_id:i for i in result.evidence}
                    observations, _ = fresh_observations(result.observations, cutoff, timedelta(days=7))
                    for obs in sorted(observations, key=lambda o:o.as_of):
                        item = by_market.get(obs.evidence_id)
                        if (obs.symbol in missing_symbols and item and
                            _observation_matches_evidence(obs,item) and
                            _pit_eligible(item,cutoff,historical=historical)):
                            other_prices[obs.symbol] = obs.price
                            selected.append(item)
                            valuation_evidence_ids.append(item.evidence_id)
                    error = None
                except ProviderError as exc:
                    error = exc.code
                calls.append(ToolCall(sequence=len(calls)+1,name="market.get_portfolio_eod",
                    arguments=None,status="error" if error else "success",started_at=began,
                    ended_at=utc(clock()),output_refs=valuation_evidence_ids,error=error))
            missing_symbols = sorted(set(missing_symbols)-set(other_prices))
            if not missing_symbols:
                nav_denominator = nav_numerator + sum((p.quantity*other_prices[p.symbol]
                    for p in snapshot.positions if p.symbol != request.symbol),Decimal(0)) + sum((a.cash for a in snapshot.accounts),Decimal(0))
                nav_weight = nav_numerator*100/nav_denominator if nav_denominator else None
            # Keep the legacy price panel/projection consistent with the complete
            # IDC denominator; preserve a single authoritative calculation value.
            old_context_id = context_item.evidence_id
            context = context.model_copy(update={"scenario_nav_weight_pct":nav_weight,
                "input_evidence_ids":tuple(dict.fromkeys((*context.input_evidence_ids,*valuation_evidence_ids)))})
            context_payload = context.model_dump(mode="json")
            context_hash = content_hash(context_payload)
            context_item = context_item.model_copy(update={"evidence_id":"E"+context_hash[7:23],
                "content_hash":context_hash,"structured_data":{"decision_context":context_payload}})
            selected = [context_item if i.evidence_id==old_context_id else i for i in selected]
            # Only normalized, frozen lineage can enable current descriptors.
            if hasattr(market,"completed_history") and not historical:
                began = utc(clock())
                try:
                    history, history_items = await asyncio.to_thread(market.completed_history,request.symbol,cutoff)
                    history = CloseHistory.model_validate(history)
                    history_cutoff = utc(clock())
                    ids = {i.evidence_id for i in history_items}
                    if any(i.symbol != request.symbol or i.evidence_type != "market_data" or
                           (i.as_of and i.as_of>history_cutoff) or i.retrieved_at>history_cutoff for i in history_items):
                        raise ValueError("history lineage PIT mismatch")
                    if set(history.source_evidence_ids)-ids or any(b.evidence_id not in ids for b in history.bars):
                        raise ValueError("history source evidence missing")
                    normalized_history = history.model_dump(mode="json")
                    if not any(i.structured_data==normalized_history and i.content_hash==content_hash(normalized_history)
                               for i in history_items):
                        raise ValueError("history values/basis do not match normalized source evidence")
                    if any(b.available_at>history_cutoff for b in history.bars):
                        raise ValueError("history evidence contains unfinished/future bars")
                    structure = market_structure(history,request.symbol,request.decision_price,history_cutoff)
                    selected.extend(history_items)
                    error = None
                except ProviderError as exc:
                    history_items,error = [],exc.code
                except ValueError:
                    history_items,error = [],"invalid_data"
                if error:
                    history = None
                    history_error = "HISTORY_"+error.upper()
                    structure = market_structure(None,request.symbol,request.decision_price,cutoff,
                        unknown_reason="HISTORY_"+error.upper())
                calls.append(ToolCall(sequence=len(calls)+1,name="market.get_completed_history",arguments=None,
                    status="error" if error else "success",started_at=began,ended_at=utc(clock()),
                    output_refs=[i.evidence_id for i in history_items],error=error))
        emit("context", context=context.model_dump(mode="json"))
        emit("research")
        errors = []
        if historical:
            event_items = store.get_persisted_event_evidence(request.symbol, cutoff, 20)
            if any(i.retrieved_at > cutoff and i.evidence_type not in {"portfolio_snapshot", "calculation"} for i in selected):
                raise TDBError("HISTORICAL_VINTAGE_UNAVAILABLE", 409)
            calls.append(ToolCall(sequence=len(calls)+1, name="evidence.get_persisted_events", arguments=None,
                status="success", started_at=started, ended_at=utc(clock()), output_refs=[i.evidence_id for i in event_items]))
        else:
            args = NewsSearchToolArgs(symbols=[request.symbol], since=cutoff-timedelta(days=30), until=cutoff, limit=12)
            research_query = getattr(getattr(request, "decision_framing", None), "research_query", None) or BUSINESS_QUERY
            args_sec = SECSearchToolArgs(symbol=request.symbol, query=research_query, forms=FORMS, as_of=cutoff, limit=6)
            # Independent provider requests run concurrently; call sequence records completion order.
            n, s = await asyncio.gather(
                retrieve("news.search", args, lambda: news.search(args.symbols, args.since, args.until, args.limit)),
                retrieve("sec.search", args_sec, lambda: sec.search(request.symbol, research_query, FORMS, cutoff, 6)))
            event_items = [*getattr(n, "evidence", []), *getattr(s, "evidence", [])]
            if hasattr(sec, "fundamentals"):
                result = await retrieve("sec.fundamentals", None, lambda: type("Result", (), {"evidence": sec.fundamentals(request.symbol, cutoff)})())
                event_items.extend(getattr(result, "evidence", []))
            errors = [f"{c.name}:{c.error}" for c in calls if c.status == "error"]
            cutoff = utc(clock())

        event_items = eligible(event_items)
        initial_gaps = evidence_gaps(event_items)
        supplemental = []
        if initial_gaps and not historical:
            emit("completion", gaps=[g.model_dump(mode="json") for g in initial_gaps])
            query = " ".join(dict.fromkeys(g.query for g in initial_gaps))
            args_sec = SECSearchToolArgs(symbol=request.symbol, query=query, forms=FORMS, as_of=cutoff, limit=6)
            operation = getattr(sec, "targeted_search", sec.search)
            extra = await retrieve("sec.targeted_search", args_sec,
                lambda: operation(request.symbol, query, FORMS, cutoff, 6))
            before = {i.evidence_id for i in event_items}
            extra_items = getattr(extra, "evidence", [])
            cutoff = utc(clock())
            event_items = eligible([*event_items, *extra_items])
            supplemental = [i.evidence_id for i in event_items if i.evidence_id not in before]
        research = ResearchCompletion(initial_gaps=initial_gaps,
            completion_attempted=bool(initial_gaps and not historical), supplemental_evidence_ids=tuple(supplemental),
            remaining_gaps=evidence_gaps(event_items),
            provider_errors=unresolved_provider_errors(calls))
        prior_hashes = {i.content_hash for i in prior.manifest.evidence} if prior else set()
        prior_items = store.get_run_evidence(prior.run_id) if prior else []
        prior_text = {" ".join((i.excerpt or i.title or "").casefold().split()) for i in prior_items
                      if i.evidence_type in {"news", "sec_filing"}}
        new = [i for i in event_items if prior and i.published_at > prior.analysis_cutoff
               and i.content_hash not in prior_hashes
               and " ".join((i.excerpt or i.title or "").casefold().split()) not in prior_text]
        retained = []
        if prior and prior.semantic:
            old = {i.evidence_id: i for i in store.get_run_evidence(prior.run_id)}
            retained = [old[eid] for eid in prior.semantic.assessment.counter_evidence_ids
                        if eid in old and old[eid].evidence_type in {"news", "sec_filing"}
                        and old[eid].published_at is not None]
        if is_idc:
            retained = eligible(retained)
        public = list({i.evidence_id: i for i in [*event_items, *retained]}.values())
        selected = list({i.evidence_id: i for i in [*selected, *public]}.values())
        facts = {
            "depth_contract": "v2", "symbol": request.symbol, "decision_price_usd": str(request.decision_price),
            "previous_close_usd": str(foundation.previous_close.price) if foundation.previous_close else None,
            "price_difference_usd": str(foundation.price_difference) if foundation.price_difference is not None else None,
            "price_difference_pct": str(context.price_difference_pct) if context.price_difference_pct is not None else None,
            "price_relation": _price_relation(request.decision_price, foundation.previous_close.price if foundation.previous_close else None),
            "decision_price_source": request.source, "thesis_owner_confirmed": bool(thesis and thesis.status.value == "USER_CONFIRMED"),
            "decision_context": context.model_dump(mode="json"), "decision_context_evidence_id": context_item.evidence_id,
            "portfolio_context": {"held": True, **context.model_dump(mode="json")},
            "thesis": thesis.model_dump(mode="json") if thesis else {},
            "review_mode": "FIRST_REVIEW" if not prior else "DELTA_REVIEW",
            "previous_review_cutoff": prior.analysis_cutoff.isoformat() if prior else None,
            "new_evidence_ids": [i.evidence_id for i in new],
            "research": research.model_dump(mode="json"),
            "current_evidence": [{"evidence_id": i.evidence_id, "title": i.title, "excerpt": (i.excerpt or "")[:1200],
                "published_at": i.published_at.isoformat(), "is_new_since_review": i in new,
                "structured_data": i.structured_data if (i.structured_data or {}).get("metrics") else None} for i in public],
            "evidence_cutoff": cutoff.isoformat()}
        if prior and not new and prior.decision_price != request.decision_price:
            # Supply price/history relation without anchoring to old action scores.
            facts["price_reassessment"] = {"previous_decision_price_usd": str(prior.decision_price),
                "company_delta": "NO_NEW_COMPANY_EVIDENCE", "thesis_change_from_price": False}
        if is_idc:
            investment = build_packet(request,context,snapshot,thesis,public,selected,cutoff=cutoff,
                structure=structure,nav_weight=nav_weight,nav_numerator=nav_numerator,
                nav_denominator=nav_denominator,missing_symbols=missing_symbols,
                valuation_evidence_ids=valuation_evidence_ids)
            # Deterministic results get a real calculation record. Cause/assumptions,
            # policy and semantic thesis state never enter this EvidenceItem.
            calculation = {"version":"idc-calculation-v1","decision_price_source":request.source,
                "portfolio_constraints":[c.model_dump(mode="json") for c in investment.portfolio_constraints],
                "market_structure":investment.market_structure.model_dump(mode="json")}
            calc_hash = content_hash(calculation)
            calc_item = EvidenceItem(evidence_id="E"+calc_hash[7:23],evidence_type="calculation",
                provider="python_decimal",symbol=request.symbol,title="价格区间与组合约束 · 确定性计算",
                as_of=cutoff,retrieved_at=utc(clock()),content_hash=calc_hash,structured_data=calculation)
            selected.append(calc_item)
            payload = investment.model_dump(mode="json",exclude={"packet_hash"})
            payload["evidence_ids"].append(calc_item.evidence_id)
            investment = InvestmentContextPacket(**payload,packet_hash=content_hash(payload))
            facts["investment_context"] = investment.model_dump(mode="json")
            facts["investment_context_evidence_id"] = calc_item.evidence_id
        if getattr(request, "decision_framing", None):
            facts["decision_framing"] = request.decision_framing.model_dump(mode="json")
            from .investigation import investigation_context, validate_history
            benchmark, benchmark_error = None, "BENCHMARK_UNAVAILABLE"
            if hasattr(market, "completed_history") and not historical:
                began = utc(clock())
                benchmark_items = []
                try:
                    candidate, benchmark_items = await asyncio.to_thread(market.completed_history, "SPY", utc(clock()))
                    benchmark = validate_history(candidate, benchmark_items, "SPY", utc(clock()))
                    existing = {i.evidence_id:i.content_hash for i in selected}
                    if any(i.evidence_id in existing and existing[i.evidence_id] != i.content_hash for i in benchmark_items):
                        raise ValueError("benchmark evidence ID collision")
                    selected.extend(benchmark_items)
                    benchmark_error = None
                except (ProviderError, ValueError) as exc:
                    benchmark = None
                    benchmark_items = []
                    benchmark_error = exc.code if isinstance(exc, ProviderError) else "invalid_data"
                calls.append(ToolCall(sequence=len(calls)+1, name="market.get_benchmark_history", arguments=None,
                    status="error" if benchmark_error else "success", started_at=began, ended_at=utc(clock()),
                    output_refs=[i.evidence_id for i in benchmark_items], error=benchmark_error))
            cutoff = utc(clock())
            facts["evidence_cutoff"] = cutoff.isoformat()
            investigation, item = investigation_context(request.decision_framing, history, cutoff,
                history_reason=history_error, benchmark=benchmark, benchmark_reason=benchmark_error)
            facts["investigation"] = investigation
            facts["retained_counter_evidence_ids"] = [i.evidence_id for i in retained]
            selected.append(item)
            for name in ("investigation.plan", "premise.verify", "context.project"):
                calls.append(ToolCall(sequence=len(calls)+1, name=name, arguments=None, status="success",
                    started_at=cutoff, ended_at=cutoff, output_refs=[item.evidence_id]))
            emit("verification", investigation=investigation)
            # Benchmark retrieval advances the clock after IDC was constructed.
            # Freeze one authoritative cutoff for the context and enclosing packet.
            facts["investment_context"] = _context_at_cutoff(
                facts["investment_context"], cutoff).model_dump(mode="json")
        packet = {"run_id": run_id, "request": request.model_dump(mode="json"), "foundation_id": foundation.decision_id,
            "prior_id": prior.decision_id if prior else None, "cutoff": cutoff.isoformat(),
            "selected": [i.model_dump(mode="json") for i in selected], "public_ids": [i.evidence_id for i in public],
            "event_ids": [i.evidence_id for i in event_items], "new_ids": [i.evidence_id for i in new],
            "retained_ids": [i.evidence_id for i in retained], "calls": [c.model_dump(mode="json") for c in calls],
            "facts": facts, "research": research.model_dump(mode="json")}
        store.freeze_decision_checkpoint(run_id, packet)
        checkpoint_id = run_id

    if prepare_only:
        emit("frozen", checkpoint_id=checkpoint_id, evidence_count=len(packet["selected"]))
        return checkpoint_id

    selected = [EvidenceItem.model_validate(i) for i in packet["selected"]]
    by_id = {i.evidence_id: i for i in selected}
    public = [by_id[eid] for eid in packet["public_ids"]]
    calls = [ToolCall.model_validate(c) for c in packet["calls"]]
    if run_id != checkpoint_id:
        now = utc(clock())
        calls.append(ToolCall(sequence=len(calls)+1, name="decision.resume_checkpoint", arguments=None,
            status="success", started_at=now, ended_at=now, output_refs=[checkpoint_id]))
    facts = packet["facts"]
    cutoff = datetime.fromisoformat(packet["cutoff"])
    investment = InvestmentContextPacket.model_validate(facts["investment_context"]) if is_idc else None
    if investment and investment.cutoff != cutoff:
        # Pre-fix Cockpit v2 checkpoints are hash-checked by Store. Preserve their
        # original bytes and exact judge input; only the new record gets aligned
        # metadata, with explicit source/derived hashes in its observable trace.
        if (not getattr(request, "decision_framing", None)
                or utc(datetime.fromisoformat(facts["evidence_cutoff"])) != cutoff
                or not {"investigation.plan", "premise.verify", "context.project"} <= {c.name for c in calls}
                or investment.cutoff > cutoff):
            raise TDBError("CHECKPOINT_CONTEXT_INVALID", 409)
        original_hash = investment.packet_hash
        investment = _context_at_cutoff(facts["investment_context"], cutoff)
        now = utc(clock())
        calls.append(ToolCall(sequence=len(calls)+1, name="decision.align_context_cutoff",
            arguments=None, status="success", started_at=now, ended_at=now,
            output_refs=[checkpoint_id, content_hash(packet), original_hash, investment.packet_hash]))
    prior = store.get_decision(packet["prior_id"]) if packet["prior_id"] else None
    thesis = foundation.manifest.thesis
    research = ResearchCompletion.model_validate(packet["research"])
    context_item = by_id[facts["decision_context_evidence_id"]]
    judge_evidence = [*public,*(i for i in selected if i.evidence_id not in packet["public_ids"])] if is_idc else [*public, context_item]
    events = []
    began = utc(clock())
    emit("judge", evidence_count=len(public), checkpoint_id=checkpoint_id)
    if not public:
        result = JudgeResult(thesis_delta=ThesisDelta(impact="INSUFFICIENT", rationale="缺少可核查的公司证据。"),
            assessment=DecisionAssessment(assessment_status="INSUFFICIENT_EVIDENCE", evidence_confidence="INSUFFICIENT",
                inference_confidence="INSUFFICIENT", abstention_reason=(
                    "已检查公司新闻与官方披露并进行有目标的补充检索，仍缺少公司经营与执行证据。"
                    if research.completion_attempted else "已检查截止时保存的证据版本，缺少公司经营与执行证据；历史分析不调用当前检索。")))
        model_name = None
        if is_idc:
            statement = ConditionedStatement(text="在这些假设成立时，仍缺少公司证据，先补充研究。" if hypothetical else
                "当前合资格公司证据不足，先补充研究。",evidence_ids=(context_item.evidence_id,),
                assumption_ids=tuple(a.assumption_id for a in investment.assumptions))
            result = result.model_copy(update={"conditional_assessment":ConditionalAssessment(intent=request.intent,
                headline=statement,scenario_reasoning=statement,binding_constraints=(statement,),
                change_conditions=statement,actual_thesis_state="UNKNOWN")})
    else:
        try:
            result = await judge.assess(facts, judge_evidence)
            if is_idc:
                validate_conditional(result.conditional_assessment,InvestmentContextPacket.model_validate(facts["investment_context"]),result.thesis_delta)
            _validate_judgment(result, judge_evidence, thesis.invalidation_conditions if thesis else [], _price_tokens(facts))
            if result.assessment.assessment_status != "ASSESSED" and not research.completion_attempted and request.as_of is None and not recovering and not frozen_input:
                # Semantic insufficiency can surface after the deterministic preflight.
                # Freeze a new packet after one targeted pass, then reassess once.
                first_events = getattr(judge, "attempt_events", lambda: ())()
                events.extend(first_events)
                gap = EvidenceGap(dimension="THESIS_CRITICAL", description=result.assessment.abstention_reason,
                                  query=TARGET_QUERY)
                emit("completion", gaps=[gap.model_dump(mode="json")])
                target_started = utc(clock())
                extra = []
                extra_error = None
                try:
                    operation = getattr(sec, "targeted_search", sec.search)
                    response = await asyncio.to_thread(operation, request.symbol, gap.query, FORMS, cutoff, 6)
                    extra = response.evidence
                except ProviderError as exc:
                    extra_error = exc.code
                cutoff = utc(clock())
                if any(i.symbol != request.symbol for i in extra):
                    raise LLMError("invalid_evidence_reference", "supplemental evidence symbol mismatch")
                extra = [i for i in extra if i.published_at is not None and i.published_at <= cutoff
                         and i.retrieved_at <= cutoff and i.evidence_id not in by_id]
                if is_idc:
                    extra = eligible(extra)
                calls.append(ToolCall(sequence=len(calls)+1, name="sec.targeted_search", arguments=
                    SECSearchToolArgs(symbol=request.symbol, query=gap.query, forms=FORMS, as_of=datetime.fromisoformat(packet["cutoff"]), limit=6),
                    status="error" if extra_error else "success", started_at=target_started, ended_at=utc(clock()),
                    output_refs=[i.evidence_id for i in extra], error=extra_error))
                public.extend(extra)
                selected.extend(extra)
                prior_texts = {" ".join((i.excerpt or i.title or "").casefold().split())
                    for i in (store.get_run_evidence(prior.run_id) if prior else [])
                    if i.evidence_type in {"news", "sec_filing"}}
                new_ids = [i.evidence_id for i in extra if prior and i.published_at > prior.analysis_cutoff
                           and i.content_hash not in {r.content_hash for r in prior.manifest.evidence}
                           and " ".join((i.excerpt or i.title or "").casefold().split()) not in prior_texts]
                research = ResearchCompletion(initial_gaps=(gap,), completion_attempted=True,
                    supplemental_evidence_ids=tuple(i.evidence_id for i in extra),
                    remaining_gaps=() if extra else (gap,), provider_errors=(extra_error,) if extra_error else ())
                facts = {**facts, "research": research.model_dump(mode="json"), "evidence_cutoff": cutoff.isoformat(),
                    "new_evidence_ids": [*packet["new_ids"], *new_ids],
                    "current_evidence": [*facts["current_evidence"], *[{"evidence_id": i.evidence_id, "title": i.title,
                        "excerpt": (i.excerpt or "")[:1200], "published_at": i.published_at.isoformat(),
                        "is_new_since_review": i.evidence_id in new_ids} for i in extra]]}
                if is_idc:
                    # Re-freeze context when a bounded completion changes evidence/cutoff.
                    old = InvestmentContextPacket.model_validate(facts["investment_context"])
                    payload = old.model_dump(mode="json",exclude={"packet_hash"})
                    updated_facts = reported_facts(public, cutoff)
                    missingness = [m for m in old.missingness if m != "REPORTED_FACTS_UNAVAILABLE"]
                    if not updated_facts:
                        missingness.append("REPORTED_FACTS_UNAVAILABLE")
                    payload.update(cutoff=cutoff.isoformat().replace("+00:00","Z"),evidence_ids=[i.evidence_id for i in selected],
                        actual_company_evidence_ids=[i.evidence_id for i in public],
                        reported_facts=[f.model_dump(mode="json") for f in updated_facts],
                        valuation_status="REPORTED_FACTS_ONLY" if updated_facts else "UNKNOWN",missingness=missingness)
                    investment = InvestmentContextPacket(**payload,packet_hash=content_hash(payload))
                    facts["investment_context"] = investment.model_dump(mode="json")
                packet = {**packet, "facts": facts, "cutoff": cutoff.isoformat(), "research": research.model_dump(mode="json"),
                    "selected": [i.model_dump(mode="json") for i in selected], "public_ids": [i.evidence_id for i in public],
                    "event_ids": [*packet["event_ids"], *[i.evidence_id for i in extra]], "new_ids": facts["new_evidence_ids"],
                    "calls": [c.model_dump(mode="json") for c in calls], "parent_checkpoint_id": checkpoint_id}
                checkpoint_id = str(uuid4())
                store.freeze_decision_checkpoint(checkpoint_id, packet)
                if extra:
                    judge_evidence = [*public,*(i for i in selected if i.evidence_id not in {p.evidence_id for p in public})] if is_idc else [*public, context_item]
                    emit("judge", evidence_count=len(public), checkpoint_id=checkpoint_id)
                    result = await judge.assess(facts, judge_evidence)
                    if is_idc:
                        validate_conditional(result.conditional_assessment,InvestmentContextPacket.model_validate(facts["investment_context"]),result.thesis_delta)
                    _validate_judgment(result, judge_evidence, thesis.invalidation_conditions if thesis else [], _price_tokens(facts))
                else:
                    # No input change, so no pointless second model request.
                    events = events[:-len(first_events)] if first_events else events
            if prior is None:
                # Current assessment is valid; there is no historical thesis delta to claim.
                from .tdb_semantic import baseline_thesis_delta
                result = result.model_copy(update={"thesis_delta": baseline_thesis_delta(public)})
            elif result.thesis_delta.impact in {"STRENGTHENED", "WEAKENED", "INVALIDATION_TRIGGERED"}:
                if not set(result.thesis_delta.evidence_ids) & set(packet["new_ids"]):
                    raise LLMError("unsupported_thesis_claim", "historical change requires new evidence")
            events.extend(getattr(judge, "attempt_events", lambda: ())() or [SynthesisEvent(model=judge.model_name,
                purpose=contract, started_at=began, ended_at=utc(clock()),
                evidence_ids=[i.evidence_id for i in judge_evidence], claim_links={})])
        except (LLMError, ValueError) as exc:
            code = exc.code if isinstance(exc, LLMError) else "schema_validation"
            calls.append(ToolCall(sequence=len(calls)+1, name="decision.judge", arguments=None, status="error",
                                 started_at=began, ended_at=utc(clock()), error=code, output_refs=[checkpoint_id]))
            events.extend(getattr(judge, "attempt_events", lambda: ())() or [SynthesisEvent(model=judge.model_name,
                purpose=contract, status="error", started_at=began, ended_at=utc(clock()),
                evidence_ids=[i.evidence_id for i in judge_evidence], claim_links={}, error=code)])
            store.record_failed_decision_run(trace(run_id, calls, selected, events), selected)
            emit("error", run_id=run_id, checkpoint_id=checkpoint_id, error=code)
            raise TDBError("DECISION_JUDGE_FAILED", 503, run_id) from None
        model_name = judge.model_name
    calls.append(ToolCall(sequence=len(calls)+1, name="decision.judge", arguments=None,
        status="success", started_at=began, ended_at=utc(clock()), output_refs=[checkpoint_id]))
    emit("brief")
    support = normalize_support(result.assessment.scores) if result.assessment.scores else None
    generated_thesis = None
    if thesis is None and result.depth and support and not hypothetical:
        depth = result.depth
        groups = {"core_drivers": [depth.baseline_thesis], "key_assumptions": [depth.business_quality],
                  "execution_risks": depth.key_risks, "invalidation_conditions": depth.change_my_mind}
        thesis = ThesisProfile(symbol=request.symbol, thesis_version_id="T"+uuid4().hex,
            as_of=cutoff, status="AI_DRAFT", generation={"model": model_name, "purpose": "decision_baseline"},
            **{key: [{"text": s.text, "evidence_ids": list(s.evidence_ids)} for s in group] for key, group in groups.items()},
            evidence_ids=list(dict.fromkeys(eid for group in groups.values() for s in group for eid in s.evidence_ids)))
        payload = thesis.model_dump(mode="json")
        digest = content_hash(payload)
        thesis_item = EvidenceItem(evidence_id="E"+digest[7:23], evidence_type="thesis", provider="decision_judge",
            symbol=request.symbol, title="首次持仓判断 · AI 基线", as_of=cutoff, retrieved_at=utc(clock()),
            source_record_id=thesis.thesis_version_id, content_hash=digest, structured_data=payload)
        generated_thesis = (thesis, thesis_item)
        selected.append(thesis_item)
    links = {"price_context": [context_item.evidence_id], "thesis_delta": list(result.thesis_delta.evidence_ids) or [context_item.evidence_id]}
    for rationale in result.assessment.rationales:
        links[f"action_{rationale.action}"] = list(rationale.evidence_ids)
    if result.price_assessment:
        from .tdb_semantic import price_statements
        for index, statement in enumerate(price_statements(result.price_assessment)):
            links[f"price_assessment_{index}"] = list(statement.evidence_ids)
    if result.depth:
        for index, statement in enumerate(depth_statements(result.depth)):
            links[f"decision_depth_{index}"] = list(statement.evidence_ids)
    if is_idc:
        for index,statement in enumerate(conditional_statements(result.conditional_assessment)):
            links[f"conditional_{index}"] = list(statement.evidence_ids)
    if result.assessment.assessment_status != "ASSESSED" and result.depth and result.depth.missing_context:
        research = research.model_copy(update={"remaining_gaps": tuple(EvidenceGap(dimension="THESIS_CRITICAL",
            description=text, query=TARGET_QUERY) for text in result.depth.missing_context)})
    artifact = SemanticArtifact(version=2, foundation_record_id=foundation.decision_id,
        foundation_run_id=foundation.run_id, evidence_scope="HISTORICAL_PERSISTED" if request.as_of else "CURRENT_30_DAY_NEWS_PLUS_SEC",
        judge_context_hash=content_hash(facts), what_changed_mode=facts["review_mode"],
        current_evidence_ids=tuple(packet["event_ids"]), new_evidence_ids=tuple(packet["new_ids"]),
        retained_counter_evidence_ids=tuple(packet["retained_ids"]), thesis_delta=result.thesis_delta,
        assessment=result.assessment, action_support=support, model_name=model_name, prompt_contract=contract,
        explanation=(result.depth.headline.text if result.depth else
            f"判断价格 {request.decision_price} 美元；" + (f"相对参考收盘价 {facts['price_difference_pct']}%。" if foundation.previous_close else "参考收盘价未知。") +
            (result.assessment.abstention_reason or "当前判断基线已建立。")),
        depth=result.depth, context_evidence_id=context_item.evidence_id, research=research,
        price_assessment=result.price_assessment)
    normalized = {"symbol": request.symbol, "decision_price": str(request.decision_price), "source": request.source,
        "currency": request.currency, "cutoff": cutoff.isoformat(), "snapshot_id": foundation.portfolio_snapshot_id,
        "snapshot_hash": foundation.portfolio_snapshot_hash, "holding_position_ids": foundation.holding_position_ids,
        "market_observation": foundation.previous_close.model_dump(mode="json") if foundation.previous_close else None,
        "thesis": thesis.model_dump(mode="json") if thesis else None, "previous_qualified_record_id": prior.decision_id if prior else None}
    if is_idc:
        normalized["investment_context_hash"] = investment.packet_hash
    mf = dict(version=2, evidence_cutoff=cutoff, snapshot_id=foundation.portfolio_snapshot_id,
        snapshot_hash=foundation.portfolio_snapshot_hash, market_observation=foundation.previous_close, thesis=thesis,
        previous_qualified_record_id=prior.decision_id if prior else None,
        evidence=tuple(EvidenceReference.from_evidence(i) for i in selected), normalized_input_hash=content_hash(normalized),
        run_id=run_id, tool_names=tuple(c.name for c in calls), provider_name=market.name,
        model_name=model_name, prompt_contract=contract, claim_links=links,
        sec_accessions={i.evidence_id: str(i.structured_data["accession_number"]) for i in selected
                       if i.evidence_type == "sec_filing" and (i.structured_data or {}).get("accession_number")})
    manifest = EvidenceManifest(**mf, manifest_hash=content_hash(EvidenceManifest.model_construct(**mf).model_dump(mode="json")))
    rf = dict(version=3 if is_idc else 2, decision_id=str(uuid4()), symbol=request.symbol, decision_price=request.decision_price,
        decision_price_source=request.source, currency=request.currency, analysis_cutoff=cutoff, recorded_at=utc(clock()),
        portfolio_snapshot_id=foundation.portfolio_snapshot_id, portfolio_snapshot_hash=foundation.portfolio_snapshot_hash,
        holding_position_ids=foundation.holding_position_ids, previous_close=foundation.previous_close,
        price_difference=foundation.price_difference, price_difference_pct=price_difference_percentage(request.decision_price,
            foundation.previous_close.price if foundation.previous_close else None), thesis_version_id=thesis.thesis_version_id if thesis else None,
        previous_qualified_record_id=prior.decision_id if prior else None,
        review_state="FIRST_REVIEW" if not prior else "PRIOR_QUALIFIED_REVIEW", record_kind=("SCENARIO_REVIEW" if support else "SCENARIO_ABSTENTION") if hypothetical else "QUALIFIED_REVIEW" if support else "SEMANTIC_ABSTENTION",
        manifest=manifest, run_id=run_id, semantic=artifact, user_decision=None, outcome_status="PENDING")
    if is_idc:
        rf.update(investment_context=investment,
                  conditional_assessment=result.conditional_assessment)
    record = DecisionRecord(**rf, record_hash=content_hash(DecisionRecord.model_construct(**rf).model_dump(mode="json")))
    store.add_decision(record, trace(run_id, calls, selected, events, links), selected, thesis_record=generated_thesis)
    emit("complete", decision_id=record.decision_id, run_id=record.run_id)
    return record
