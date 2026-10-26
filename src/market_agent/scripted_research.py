"""A real, keyless research path that a trace can honestly describe.

This is a demonstration harness, not a product surface. Its only purpose is to give
the trace recorder something *actually executed* to record: real SEC metadata through
the real capture adapter, the real filing projection, the real strict prior-Thesis
read, and real ``Decimal`` arithmetic. Nothing here calls a model, opens a socket or
touches the Owner database.

Labelling is part of the contract. A run of this module is:

- real: the public SEC filing metadata (a frozen, hash-bound capture) and the
  deterministic code paths that normalise, project and select it;
- synthetic: the portfolio, the prior Thesis and the hypothetical price;
- scripted: the "synthesis" step, which is a deterministic template and is recorded
  as ``SCRIPTED`` with zero model calls and no token counts.

The refusal path is equally real: it calls the production ``admit_model_send`` gate
with no server-side consent receipt and records the exception that gate really raises.
That is why a blocked trace here is evidence about the gate rather than a claim that
the gate exists.

``scenario_filing_research`` is replayable: it rebuilds its isolated store and reseeds
it deterministically, so re-executing it must reproduce the same outcome digest.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .domain import (
    EvidenceItem,
    PortfolioContent,
    PortfolioSnapshot,
    portfolio_content_hash,
    utc,
)
from .run_trace import (
    PolicyDecision,
    SourceRef,
    TraceRecorder,
    TraceRun,
)
from .sec_filings import CapturedFilingsSource
from .sec_filings import project as project_filings
from .sec_thesis_delta import project_one as project_thesis_delta

SCENARIO_ID = "SEC-FILING-TO-THESIS-CHECK"
REFUSAL_SCENARIO_ID = "OWNER-CONTEXT-FORWARD-WITHOUT-CONSENT"
SYNTHETIC_LABEL = "SYNTHETIC_NOT_OWNER_DATA"

# One synthetic portfolio. Deliberately not the Owner's: a keyless demonstration must
# not depend on, read or export real positions.
SYNTHETIC_LOTS = (
    {"account_id": "syn-a", "symbol": "GLXY", "quantity": "12", "avg_cost": "21.40"},
    {"account_id": "syn-b", "symbol": "GLXY", "quantity": "8", "avg_cost": "19.75"},
)

SYNTHETIC_THESIS = {
    "core_drivers": [
        "代币化股票（Tokenized GLXY）若被采用，可能改善结算效率与可及性。",
        "董事会授权的 2 亿美元 A 类普通股回购计划支撑每股价值。",
    ],
    "key_assumptions": [
        "代币化证券市场能形成有流动性的有序市场。",
        "回购的时点与规模按计划推进。",
    ],
    "execution_risks": [
        "数字过户代理的准入要求可能限制参与者与市场深度。",
        "跨平台流动性碎片化可能扩大买卖价差。",
    ],
    "invalidation_conditions": [
        "监管要求暂停、修改或清退 Tokenized GLXY。",
        "回购计划被中止或显著缩水。",
    ],
}


def synthetic_portfolio(as_of: datetime) -> PortfolioSnapshot:
    """A labelled synthetic portfolio, never derived from Owner data."""
    content = PortfolioContent(
        schema_version=1,
        portfolio_id="synthetic-demo",
        base_currency="USD",
        as_of=utc(as_of),
        accounts=(
            {"account_id": "syn-a", "name": "SYNTHETIC-A", "cash": "0"},
            {"account_id": "syn-b", "name": "SYNTHETIC-B", "cash": "0"},
        ),
        positions=tuple(
            {"position_id": f'{lot["account_id"]}:{lot["symbol"]}', **lot,
             "user_stage": "hold", "note": SYNTHETIC_LABEL}
            for lot in SYNTHETIC_LOTS),
        watchlist=(),
        instrument_overrides={},
        source={"kind": "manual_entry", "imported_at": utc(as_of).isoformat(),
                "source_hash": "sha256:" + "0" * 64, "legacy_manual_prices": {}},
    )
    return PortfolioSnapshot(snapshot_id="synthetic-demo",
                             content_hash=portfolio_content_hash(content),
                             **content.model_dump())


def exact_decimal(value: str) -> Decimal:
    """Exact decimal from a string. A float input is refused, never rounded."""
    if isinstance(value, float):
        raise TypeError("float money is not accepted; pass an exact decimal string")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - defensive
        raise ValueError("invalid_decimal") from exc
    if not parsed.is_finite():
        raise ValueError("invalid_decimal")
    return parsed


def seed_synthetic_thesis(store, symbol: str, as_of: datetime) -> dict[str, Any]:
    """Write one deterministic prior Thesis into an isolated store."""
    from .thesis import (
        ThesisDraftOutput,
        ThesisGenerationMetadata,
        ThesisProfile,
        ThesisStatement,
        ThesisStatus,
    )

    draft = ThesisDraftOutput(**{
        key: [ThesisStatement(text=text, evidence_ids=["Esyntheticvaluefixture"])
              for text in texts]
        for key, texts in SYNTHETIC_THESIS.items()
    } | {"evidence_ids": ["Esyntheticvaluefixture"]})
    payload = {"symbol": symbol, "as_of": utc(as_of).isoformat(),
               "draft": draft.model_dump(mode="json"), "model": SYNTHETIC_LABEL}
    digest = "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    version_id = "TV" + digest[7:23]
    profile = ThesisProfile(
        **draft.model_dump(), symbol=symbol, thesis_version_id=version_id, as_of=utc(as_of),
        status=ThesisStatus.AI_DRAFT,
        generation=ThesisGenerationMetadata(model=SYNTHETIC_LABEL))
    evidence = EvidenceItem(
        evidence_id="E" + digest[7:23], evidence_type="thesis",
        provider="synthetic_fixture", symbol=symbol,
        title="SYNTHETIC 投资逻辑（示例，不是 Owner 记录）",
        source_record_id=version_id, retrieved_at=utc(as_of) + timedelta(minutes=5),
        as_of=utc(as_of), content_hash=digest,
        structured_data=profile.model_dump(mode="json"))
    store.add_thesis(profile, evidence)
    return {"thesis_version_id": version_id, "as_of": utc(as_of).isoformat(),
            "status": "AI_DRAFT", "label": SYNTHETIC_LABEL}


def _consolidate(portfolio: PortfolioSnapshot, symbol: str) -> dict[str, Any]:
    lots = [p for p in portfolio.positions if p.symbol == symbol]
    quantity = sum((p.quantity for p in lots), Decimal("0"))
    cost = sum((p.quantity * p.avg_cost for p in lots), Decimal("0"))
    return {"symbol": symbol, "lots": len(lots), "quantity": quantity, "cost_basis": cost}


def run_filing_research(*, capture: dict, as_of: datetime, symbol: str,
                        hypothetical_price: str, run_id: str = "trace-filing-001",
                        store_path: Path | None = None,
                        thesis_as_of: datetime | None = None) -> TraceRun:
    """Execute the real keyless chain and record what actually happened."""
    from .persistence import Store

    as_of = utc(as_of)
    thesis_as_of = utc(thesis_as_of or (as_of - timedelta(days=60)))
    trace = TraceRecorder(
        run_id=run_id, execution_mode="KEYLESS_SCRIPTED", scenario_id=SCENARIO_ID,
        as_of=as_of, pit_cutoff=as_of, task_id=run_id)
    portfolio = synthetic_portfolio(as_of)
    holder: dict[str, Any] = {"quantity": Decimal("0"), "cost_basis": Decimal("0")}

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(store_path or (Path(tmp) / "trace-demo.db"))
        seed_synthetic_thesis(store, symbol, thesis_as_of)

        with trace.record_tool("portfolio.synthetic_snapshot",
                               input_class={"symbol": symbol,
                                            "as_of": as_of.isoformat()}) as span:
            consolidated = _consolidate(portfolio, symbol)
            span.output_count = consolidated["lots"]
            holder.update(quantity=consolidated["quantity"],
                          cost_basis=consolidated["cost_basis"])

        # Applicability is a real gate: an unheld symbol is not researchable here.
        held = holder["quantity"] > 0
        trace.record_gate("APPLICABILITY", decision="ALLOW" if held else "DENY",
                          code=None if held else "not_held",
                          detail=f"{consolidated['lots']} synthetic lot(s)")
        if not held:
            return trace.close(status="BLOCKED", error_code="not_held")

        source = CapturedFilingsSource(capture)
        with trace.record_tool("sec.recent_filings",
                               input_class={"symbol": symbol, "as_of": as_of.isoformat(),
                                            "limit": 40}) as span:
            recent = source.recent_filings(symbol, as_of=as_of)
            span.output_count = len(recent.rows)
            span.sources.append(SourceRef(
                source_id=recent.issuer.cik, kind="SEC_EDGAR_SUBMISSIONS",
                captured_at=recent.retrieved_at, content_hash=capture.get("raw_metadata_hash"),
                rights="PUBLIC_FILING_METADATA", pit_ok=True))

        with trace.record_tool("sec_filings.project",
                               input_class={"symbol": symbol, "as_of": as_of.isoformat(),
                                            "lookback_days": 60}) as span:
            projection = project_filings(source, as_of=as_of, scopes=[(symbol, "HOLDING")],
                                         lookback_days=60, capture_identity="frozen-capture")
            span.output_count = len(projection.filings)
            span.evidence_ids = [f.evidence_id for f in projection.filings]
        if not projection.filings or not projection.ranked_evidence_ids:
            trace.record_gate("CONTRACT", decision="DENY", code="no_eligible_filing")
            return trace.close(status="BLOCKED", error_code="no_eligible_filing")

        # Deterministic selection: the projection's own ranking, not a judgement here.
        chosen = next(f for f in projection.filings
                      if f.evidence_id == projection.ranked_evidence_ids[0])

        pit_ok = chosen.available_at <= as_of
        trace.record_gate("PIT", decision="ALLOW" if pit_ok else "DENY",
                          code=None if pit_ok else "pit_breach",
                          detail=f"available_at {chosen.available_at.isoformat()}")
        if not pit_ok:
            return trace.close(status="BLOCKED", error_code="pit_breach")

        with trace.record_tool("sec.thesis_delta",
                               input_class={"symbol": symbol, "as_of": as_of.isoformat(),
                                            "accession": chosen.accession}) as span:
            delta = project_thesis_delta(chosen, store)
            span.output_count = len(delta.statements)
            span.evidence_ids = [chosen.evidence_id]
            if delta.thesis_version_id:
                span.notes.append(f"thesis:{delta.thesis_version_id}:{delta.status}")
            span.notes.append(f"cutoff:{delta.cutoff.basis}")

        # Deterministic synthesis: a template over recorded facts, not a model.
        trace.record_scripted(
            "research_summary",
            prompt_contract="mia-value-demo-template-v1",
            notes=[f"event:{chosen.event_class}", f"thesis:{delta.status}",
                   "no model call; text is a fixed template over recorded fields"])

        try:
            with trace.record_tool("whatif.decimal",
                                   input_class={"symbol": symbol, "as_of": as_of.isoformat(),
                                                "kind": "HYPOTHETICAL_PRICE"}) as span:
                price = exact_decimal(hypothetical_price)
                if price <= 0:
                    raise ValueError("invalid_price")
                value = (holder["quantity"] * price).quantize(Decimal("0.01"))
                basis = holder["cost_basis"].quantize(Decimal("0.01"))
                span.output_count = 1
                span.notes.append(f"pnl_sign:{'POSITIVE' if value > basis else 'NON_POSITIVE'}")
        except (ValueError, TypeError):
            trace.record_gate("CONTRACT", decision="DENY", code="invalid_price")
            return trace.close(status="BLOCKED", error_code="invalid_price")

        trace.record_gate("RIGHTS", decision="ALLOW", code="PUBLIC_METADATA_ONLY",
                          detail="public filing metadata; no third-party body")
        trace.record_gate("BUDGET", decision="ALLOW", code="no_paid_dispatch",
                          detail="keyless: zero provider calls")

    run = trace.close(status="COMPLETE")
    return run.model_copy(update={"limitations": [
        "冻结的真实公开 SEC 元数据 + 合成组合/合成 Thesis + 脚本化合成（零模型调用）。",
        "不是投资结论，不含盈利影响或股价因果判断。",
    ]})


def run_refused_forward(*, run_id: str = "trace-refusal-001",
                        as_of: datetime | None = None) -> TraceRun:
    """Trigger the production consent gate without a receipt and record the refusal.

    This is the negative control the eval suite scores: the gate is the real
    ``admit_model_send``, the exception is the one it really raises, and no socket is
    opened because the gate refuses before any transport is selected.
    """
    from .llm import LLMError
    from .research_egress import admit_model_send

    as_of = utc(as_of or datetime(2026, 10, 26, tzinfo=timezone.utc))
    trace = TraceRecorder(
        run_id=run_id, execution_mode="KEYLESS_SCRIPTED", scenario_id=REFUSAL_SCENARIO_ID,
        as_of=as_of, pit_cutoff=as_of, task_id=run_id)
    facts = {"symbol": "GLXY", "classes": ["OWNED_PORTFOLIO", "PUBLIC_FILINGS"]}

    # The attempt span derives BLOCKED from the exception the production gate raises.
    try:
        with trace.span("gate", "CONSENT",
                        input_class={"provider": "openai", "purpose": "research.synthesis"}):
            admit_model_send("openai", "gpt-demo", "https://api.example.invalid/v1/chat",
                             facts, purpose="research.synthesis")
    except LLMError as exc:
        # The decision itself is a separate, explicit record: the gate said no.
        with trace.span("gate", "CONSENT",
                        input_class={"provider": "openai",
                                     "purpose": "research.synthesis"}) as span:
            span.policies.append(PolicyDecision(
                gate="CONSENT", decision="DENY", code=exc.code,
                detail="no server-side consent receipt bound to task hash"))
            span.status = "BLOCKED"
        return trace.close(status="BLOCKED", error_code=exc.code)

    trace.record_gate("CONSENT", decision="ALLOW", code="unexpected")
    return trace.close(status="COMPLETE")


def scenario_filing_research(capture: dict, *, as_of: datetime, symbol: str,
                             hypothetical_price: str,
                             run_id: str = "trace-filing-001") -> TraceRun:
    """Replay entry point: rebuild every input and re-execute the real chain."""
    return run_filing_research(capture=capture, as_of=as_of, symbol=symbol,
                               hypothetical_price=hypothetical_price, run_id=run_id)
