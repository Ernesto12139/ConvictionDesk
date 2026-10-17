"""Bounded investigation and descriptive premise checks, never trading signals."""
import re
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Literal

from pydantic import Field

from .domain import EvidenceItem, FrozenModel, utc
from .investment_context import CloseHistory, market_structure
from .tdb import content_hash


class UserClaim(FrozenModel):
    claim_id: str
    kind: Literal["USER_OBSERVATION", "USER_MARKET_PRICE", "USER_CLAIM", "USER_OBJECTIVE"]
    text: str = Field(min_length=1, max_length=500)
    price_state: Literal["SIDEWAYS", "UPTREND", "DOWNTREND", "SHARP_DROP", "REBOUND"] | None = None
    scope: Literal["CURRENT_CLAIM", "HYPOTHETICAL_ASSUMPTION"] = "CURRENT_CLAIM"


class InvestigationPlan(FrozenModel):
    version: Literal["investigation-v1"] = "investigation-v1"
    claims_to_verify: tuple[str, ...]
    required_tools: tuple[str, ...]
    optional_tools: tuple[str, ...] = ("market.get_reference", "market.get_benchmark_history")
    max_provider_attempts: Literal[16] = 16
    company_evidence_budget: Literal[20] = Field(default=20, description="New company items; previously selected counterevidence is carried separately, never silently dropped")
    stop_conditions: tuple[str, ...] = ("one targeted completion at most", "no repeated model research loop",
        "provider failure remains missing", "freeze before decision")


class PriceDescriptor(FrozenModel):
    sessions: Literal[20, 60]
    status: Literal["AVAILABLE", "UNKNOWN"]
    reason: str | None = None
    recent_return_pct: Decimal | None = None
    close_range_pct: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    daily_realized_volatility_pct: Decimal | None = None
    normalized_slope_pct_per_session: Decimal | None = None
    last_close_change_pct: Decimal | None = None
    price_state: Literal["SIDEWAYS", "UPTREND", "DOWNTREND", "VOLATILE_RANGE"] | None = None
    evidence_ids: tuple[str, ...] = ()


class TechnicalContext(FrozenModel):
    version: Literal["price-descriptors-v1"] = "price-descriptors-v1"
    windows: tuple[PriceDescriptor, ...]
    definition: str = ("Provided completed closes only. SIDEWAYS: absolute window return <=5% and "
        "close range/start <=12%; UP/DOWN: window return >5%/<-5%; otherwise VOLATILE_RANGE. "
        "Sharp drop/rebound checks use last close change <=-5%/>=3%. Descriptive conventions, "
        "not support/resistance, trading signals, policy or intraday verification.")


class PremiseVerification(FrozenModel):
    claim_id: str
    status: Literal["SUPPORTED", "PARTIALLY_SUPPORTED", "CONTRADICTED", "NOT_VERIFIABLE"]
    explanation: str
    evidence_ids: tuple[str, ...] = ()


def classify_claims(query):
    claims = []
    for text in re.split(r"[，,。！？!?;；\n]", query):
        text = text.strip()
        if not text:
            continue
        state = next((state for words, state in (
            (("横盘", "sideways"), "SIDEWAYS"), (("上涨趋势", "上升趋势"), "UPTREND"),
            (("下跌趋势", "下降趋势"), "DOWNTREND"), (("急跌", "暴跌"), "SHARP_DROP"),
            (("反弹", "rebound"), "REBOUND")) if any(w in text.lower() for w in words)), None)
        if re.search(r"(?:跌到|涨到|现价|\$|股价)\s*\d", text):
            kind = "USER_MARKET_PRICE"
        elif state or any(w in text for w in ("官司", "诉讼", "指引", "财报", "监管", "合同")):
            kind = "USER_CLAIM"
        elif any(w in text.lower() for w in ("是否", "吗", "能不能", "什么变化", "要不要", "worth", "should")):
            kind = "USER_OBJECTIVE"
        else:
            kind = "USER_OBSERVATION"
        claims.append(UserClaim(claim_id=f"U:claim-{len(claims)+1}", kind=kind, text=text[:500], price_state=state))
    if not any(c.kind == "USER_OBJECTIVE" for c in claims) and any(w in query for w in ("是否", "吗", "能不能", "要不要")):
        claims.append(UserClaim(claim_id="U:objective", kind="USER_OBJECTIVE", text=query[:500]))
    return tuple(claims[:8])


def investigation_plan(framing):
    claims = classify_claims(framing.query)
    if framing.intent=="HYPOTHETICAL":
        claims=tuple(c.model_copy(update={'scope':'HYPOTHETICAL_ASSUMPTION'}) if c.kind!='USER_OBJECTIVE' and (
            re.search(r'假设|如果|假如|若是|\bsuppose\b|\bwhat if\b',c.text,re.I) or
            any(c.text in a.text for a in framing.assertions)) else c for c in claims)
    tools = tuple(t for t in framing.tool_plan if t != "decision.judge")
    if framing.workflow in {"IDC", "EVENT_DECISION"}:
        tools = (*tools, "market.get_completed_history", "premise.verify", "context.project")
    return claims, InvestigationPlan(claims_to_verify=tuple(c.claim_id for c in claims if c.kind != "USER_OBJECTIVE"),
        required_tools=tuple(dict.fromkeys(tools)))


def validate_history(history, items, symbol, cutoff):
    history = CloseHistory.model_validate(history)
    cutoff = utc(cutoff)
    ids = {i.evidence_id for i in items}
    if history.symbol != symbol or any(i.symbol != symbol or i.evidence_type != "market_data" or
        (i.as_of and i.as_of > cutoff) or i.retrieved_at > cutoff for i in items):
        raise ValueError("history lineage mismatch")
    if set(history.source_evidence_ids) - ids or any(b.evidence_id not in ids or b.available_at > cutoff for b in history.bars):
        raise ValueError("history source/PIT mismatch")
    normalized = history.model_dump(mode="json")
    if not any(i.structured_data == normalized and i.content_hash == content_hash(normalized) for i in items):
        raise ValueError("history normalization hash mismatch")
    # Incumbent freshness, basis, calendar and premature-session validation.
    market_structure(history, symbol, Decimal(1), cutoff)
    return history


def technical_context(history, symbol, cutoff, *, reason=None):
    structure = market_structure(history, symbol, Decimal(1), cutoff, unknown_reason=reason)
    windows = []
    for row in structure.windows[:2]:
        if row.status != "AVAILABLE":
            windows.append(PriceDescriptor(sessions=row.sessions, status="UNKNOWN", reason=row.reason))
            continue
        bars = sorted((b for b in history.bars if b.available_at <= cutoff), key=lambda b: b.session_date)[-row.sessions:]
        closes = [b.close for b in bars]
        with localcontext() as ctx:
            ctx.prec = 34
            returns = [(b/a-1)*100 for a, b in zip(closes, closes[1:])]
            change = (closes[-1]/closes[0]-1)*100
            spread = (max(closes)-min(closes))/closes[0]*100
            peak = closes[0]
            drawdown = Decimal(0)
            for close in closes:
                peak = max(peak, close)
                drawdown = min(drawdown, (close/peak-1)*100)
            mean = sum(returns)/Decimal(len(returns))
            volatility = (sum((r-mean)**2 for r in returns)/Decimal(len(returns)-1)).sqrt()
            xs = [Decimal(i) for i in range(len(closes))]
            xmean = sum(xs)/Decimal(len(xs))
            ymean = sum(closes)/Decimal(len(closes))
            slope = sum((x-xmean)*(y-ymean) for x,y in zip(xs,closes))/sum((x-xmean)**2 for x in xs)
            state = "UPTREND" if change > 5 else "DOWNTREND" if change < -5 else "SIDEWAYS" if spread <= 12 else "VOLATILE_RANGE"
            windows.append(PriceDescriptor(sessions=row.sessions, status="AVAILABLE", recent_return_pct=change,
                close_range_pct=spread, max_drawdown_pct=drawdown, daily_realized_volatility_pct=volatility,
                normalized_slope_pct_per_session=slope/closes[0]*100, last_close_change_pct=returns[-1],
                price_state=state, evidence_ids=row.evidence_ids))
    return TechnicalContext(windows=tuple(windows))


def verify_premises(claims, technical):
    checks = []
    for claim in claims:
        if claim.kind == "USER_OBJECTIVE":
            continue
        if claim.scope=="HYPOTHETICAL_ASSUMPTION":
            checks.append(PremiseVerification(claim_id=claim.claim_id,status="NOT_VERIFIABLE",
                explanation="这是你明确提出的假设，未当作已发生事实；完成收盘历史仅提供当前背景，不否定本次情景前提。"))
            continue
        if not claim.price_state or claim.kind == "USER_MARKET_PRICE":
            checks.append(PremiseVerification(claim_id=claim.claim_id, status="NOT_VERIFIABLE",
                explanation="用户提供的信息保留为观察或待核实前提；检索材料不自动证明事件结果或盘前价格。"))
            continue
        windows = technical.windows if "很久" in claim.text else technical.windows[:1]
        available = [w for w in windows if w.status == "AVAILABLE"]
        if not available:
            checks.append(PremiseVerification(claim_id=claim.claim_id, status="NOT_VERIFIABLE", explanation="缺少合资格的完成交易日历史，无法核实价格状态。"))
            continue
        outcomes = []
        for w in available:
            supported = w.price_state == claim.price_state
            if claim.price_state == "SHARP_DROP": supported = w.last_close_change_pct <= -5
            if claim.price_state == "REBOUND": supported = w.last_close_change_pct >= 3
            partial = claim.price_state == "SIDEWAYS" and w.price_state == "VOLATILE_RANGE"
            outcomes.append("SUPPORTED" if supported else "PARTIALLY_SUPPORTED" if partial else "CONTRADICTED")
        status = "CONTRADICTED" if "CONTRADICTED" in outcomes else "PARTIALLY_SUPPORTED" if (
            "PARTIALLY_SUPPORTED" in outcomes or len(available) != len(windows)) else "SUPPORTED"
        labels = {"SIDEWAYS":"横盘", "UPTREND":"上涨", "DOWNTREND":"下行", "VOLATILE_RANGE":"高波动区间"}
        summary = "；".join(f"近{w.sessions}个已提供完成交易日收盘变化{w.recent_return_pct:.2f}%，描述为{labels[w.price_state]}" for w in available)
        explanation = f"你提到‘{claim.text}’，但{summary}；下面按查证后的价格结构分析。" if status == "CONTRADICTED" else (
            f"{summary}；仅核实这一时间窗口，不证明盘中状态或更长时期。")
        checks.append(PremiseVerification(claim_id=claim.claim_id, status=status, explanation=explanation,
            evidence_ids=tuple(dict.fromkeys(e for w in available for e in w.evidence_ids))))
    return tuple(checks)


def investigation_context(framing, history, cutoff, *, history_reason=None, benchmark=None, benchmark_reason=None):
    claims, plan = investigation_plan(framing)
    technical = technical_context(history, framing.symbol, cutoff, reason=history_reason)
    checks = verify_premises(claims, technical)
    data = {"user_claims":[c.model_dump(mode="json") for c in claims], "plan":plan.model_dump(mode="json"),
        "technical":technical.model_dump(mode="json"), "premise_verification":[c.model_dump(mode="json") for c in checks],
        "benchmark":technical_context(benchmark,"SPY",cutoff,reason=benchmark_reason).model_dump(mode="json"),
        "benchmark_scope":"SPY completed closes only; no same-session premarket attribution or sector mapping"}
    # Only deterministic observed market descriptors enter the calculation evidence.
    calculated = {k:data[k] for k in ("technical", "benchmark", "benchmark_scope")}
    digest = content_hash(calculated)
    item = EvidenceItem(evidence_id="E"+digest[7:23], evidence_type="calculation", provider="python_decimal",
        symbol=framing.symbol, title="完成收盘价格描述与市场对照", as_of=cutoff, retrieved_at=cutoff,
        content_hash=digest, structured_data=calculated)
    data["calculation_evidence_id"] = item.evidence_id
    return data, item
