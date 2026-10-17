"""Decision scenarios, never a replacement for normalized market observations."""
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import Field, field_validator

from .domain import FrozenModel, EvidenceItem, utc
from .provider import MAX_EOD_AGE, ProviderError
from .tdb import content_hash, price_difference_percentage


class MarketReference(FrozenModel):
    price: Decimal = Field(gt=0)
    as_of: datetime
    retrieved_at: datetime
    provider: str
    evidence_id: str
    basis: Literal["LAST_TRADE", "MINUTE_CLOSE", "QUOTE_CURRENT"]
    session: str = "UNKNOWN"
    freshness: Literal["LATEST_AVAILABLE"] = "LATEST_AVAILABLE"
    _utc_as_of = field_validator("as_of")(utc)
    _utc_retrieved = field_validator("retrieved_at")(utc)


class DecisionContext(FrozenModel):
    decision_price_usd: Decimal
    decision_price_source: Literal["USER_INPUT", "COMPLETED_CLOSE"] = "USER_INPUT"
    previous_close_usd: Decimal | None
    previous_close_as_of: datetime | None
    price_difference_pct: Decimal | None
    market_reference_difference_pct: Decimal | None = None
    market_reference: MarketReference | None = None
    market_reference_status: str
    quantity: Decimal
    average_cost_usd: Decimal
    cost_basis_usd: Decimal
    scenario_value_usd: Decimal
    scenario_unrealized_pnl_usd: Decimal
    scenario_unrealized_pnl_pct: Decimal | None
    break_even_change_pct: Decimal
    portfolio_cost_exposure_pct: Decimal | None
    scenario_nav_weight_pct: Decimal | None = None
    limitations: tuple[str, ...]
    input_evidence_ids: tuple[str, ...]


def context_evidence(request, foundation, snapshot, reference=None, reference_status="UNAVAILABLE", *, now):
    held = [p for p in snapshot.positions if p.symbol == request.symbol]
    quantity = sum((p.quantity for p in held), Decimal(0))
    cost = sum((p.quantity * p.avg_cost for p in held), Decimal(0))
    total_cost = sum((p.quantity * p.avg_cost for p in snapshot.positions), Decimal(0))
    cash = sum((a.cash for a in snapshot.accounts), Decimal(0))
    value = quantity * request.decision_price
    close = foundation.previous_close
    refs = [r.evidence_id for r in foundation.manifest.evidence if r.evidence_type in {"portfolio_snapshot", "market_data"}]
    if reference:
        refs.append(reference.evidence_id)
    # No denominator from a partially priced portfolio. Cost exposure is separately labelled.
    one_symbol = {p.symbol for p in snapshot.positions} == {request.symbol}
    context = DecisionContext(
        decision_price_usd=request.decision_price,
        decision_price_source=request.source,
        previous_close_usd=close.price if close else None,
        previous_close_as_of=close.as_of if close else None,
        price_difference_pct=price_difference_percentage(request.decision_price, close.price if close else None),
        market_reference_difference_pct=price_difference_percentage(request.decision_price, reference.price if reference else None),
        market_reference=reference, market_reference_status=reference_status,
        quantity=quantity, average_cost_usd=cost / quantity, cost_basis_usd=cost,
        scenario_value_usd=value, scenario_unrealized_pnl_usd=value-cost,
        scenario_unrealized_pnl_pct=(value-cost)*100/cost if cost else None,
        break_even_change_pct=(cost/quantity-request.decision_price)*100/request.decision_price,
        portfolio_cost_exposure_pct=cost*100/total_cost if total_cost else None,
        scenario_nav_weight_pct=value*100/(value+cash) if one_symbol and value+cash else None,
        limitations=("成本占比不是市值仓位权重。", "缺少完整组合行情时不计算净值权重。",
                     "判断价是用户情景；成本不是合理价值。" if request.source == "USER_INPUT" else
                     "判断价使用合资格收盘参考；不是盘中行情。", "没有可靠公允价值或仓位政策阈值。"),
        input_evidence_ids=tuple(refs))
    payload = context.model_dump(mode="json")
    digest = content_hash(payload)
    evidence = EvidenceItem(evidence_id="E"+digest[7:23], evidence_type="calculation",
        provider="python_decimal", symbol=request.symbol, title="判断价格与持仓情景",
        retrieved_at=now, as_of=foundation.analysis_cutoff, content_hash=digest,
        structured_data={"decision_context": payload})
    return context, evidence


def retrieve_reference(market, symbol, now):
    """Only called for an explicit current request, never for historical cutoffs."""
    if getattr(market, "name", "") != "massive":
        return None, None, "UNAVAILABLE"
    path = f"/v2/snapshot/locale/us/markets/stocks/tickers/{quote(symbol, safe='')}"
    url = market.base_url + path
    try:
        response = market.client.get(url, headers={"Authorization": f"Bearer {market.api_key}"})
        if response.status_code in (401, 403):
            return None, None, "NOT_ENTITLED"
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderError("retryable", "snapshot unavailable")
        if response.status_code != 200:
            return None, None, "UNAVAILABLE"
        raw = json.loads(response.text, parse_float=Decimal)
        now = utc(market.clock())
        ticker = raw["ticker"]
        if raw.get("status") != "OK" or ticker.get("ticker") != symbol:
            raise ValueError("snapshot identity")
        candidates = []
        for key, price_key, scale, basis in [("lastTrade", "p", 10**9, "LAST_TRADE"),
                                             ("min", "c", 1000, "MINUTE_CLOSE")]:
            row = ticker.get(key) or {}
            if row.get(price_key) is not None and row.get("t"):
                stamp = datetime.fromtimestamp(int(row["t"])/scale, timezone.utc)
                price = Decimal(str(row[price_key]))
                if stamp <= now and price.is_finite() and price > 0:
                    candidates.append((stamp, price, basis))
        if not candidates:
            return None, None, "NO_TIMESTAMPED_PRICE"
        stamp, price, basis = max(candidates)
        # Reuse the existing freshness window, not an investment-price threshold.
        if now - stamp > MAX_EOD_AGE:
            return None, None, "STALE"
        session = "UNKNOWN"
        try:
            status = market.client.get(market.base_url+"/v1/marketstatus/now",
                headers={"Authorization": f"Bearer {market.api_key}"})
            if status.status_code == 200:
                state = status.json()
                session = ("AFTER_HOURS" if state.get("afterHours") else
                           "PRE_MARKET" if state.get("earlyHours") else str(state.get("market", "UNKNOWN")).upper())
        except (httpx.RequestError, ValueError):
            pass
        payload = {"symbol": symbol, "price": str(price), "as_of": stamp.isoformat(),
                   "provider": "massive", "currency": "USD", "basis": basis, "session": session}
        digest = content_hash(payload)
        eid = "E"+digest[7:23]
        retrieved = utc(market.clock())
        reference = MarketReference(price=price, as_of=stamp, retrieved_at=retrieved,
            provider="massive", evidence_id=eid, basis=basis, session=session)
        evidence = EvidenceItem(evidence_id=eid, evidence_type="market_data", provider="massive",
            symbol=symbol, title="最新可用市场参考 · 可能延迟", source_uri=url,
            as_of=stamp, retrieved_at=retrieved, content_hash=digest, structured_data=payload)
        return reference, evidence, "AVAILABLE"
    except httpx.RequestError:
        raise ProviderError("transport", "snapshot transport failure") from None
    except (ValueError, KeyError, TypeError, OverflowError):
        return None, None, "INVALID_DATA"
