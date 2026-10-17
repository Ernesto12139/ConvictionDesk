"""Read-only display formatting over deterministic portfolio calculations."""

from decimal import Decimal
from datetime import datetime

from .analytics import compute_portfolio_overview
from .domain import PortfolioSnapshot, PriceObservation, RunRecord


COLORS = ("#176f5b", "#4f8f78", "#91ad7f", "#d0a85c", "#9b6a55", "#66839a",
          "#786a93", "#b7b7a4", "#375f51", "#c87c63")


def _money(value: Decimal | None) -> str:
    return "未知" if value is None else f"${value:,.2f}"


def _number(value: Decimal) -> str:
    return f"{value:f}".rstrip("0").rstrip(".") if "." in f"{value:f}" else f"{value:f}"


def _percent(value: Decimal | None) -> str:
    return "未知" if value is None else f"{value * Decimal(100):.2f}%"


def _allocation(values: dict[str, Decimal]) -> tuple[list[dict], str]:
    if not values:
        return [], "none"
    ranked = sorted(values.items(), key=lambda item: (-item[1], item[0]))
    remainder = ranked[5:] if len(ranked) > 6 else []
    if remainder:
        ranked = ranked[:5] + [("其它", sum((weight for _, weight in remainder), Decimal(0)))]
    rows = []
    gradient = []
    cursor = Decimal(0)
    for index, (label, weight) in enumerate(ranked):
        end = cursor + weight * Decimal(100)
        color = COLORS[index % len(COLORS)]
        rows.append({"label": label, "weight": f"{weight * Decimal(100):.2f}%",
                     "color": color,
                     "members": [{"label": name, "weight": _percent(value)}
                                 for name, value in remainder] if remainder and index == 5 else []})
        gradient.append(f"{color} {cursor:.4f}% {end:.4f}%")
        cursor = end
    return rows, "conic-gradient(" + ", ".join(gradient) + ")"


def portfolio_overview(snapshot: PortfolioSnapshot, observations: list[PriceObservation],
                       *, cutoff: datetime, provider_error: str | None = None) -> dict:
    """Format deterministic overview metrics for the browser."""
    metrics = compute_portfolio_overview(snapshot, observations)
    symbol_allocation, symbol_gradient = _allocation(metrics.symbol_allocation)
    sectors, sector_gradient = _allocation(metrics.sector_allocation)
    themes, theme_gradient = _allocation(metrics.theme_allocation)
    position_rows = [{
        "symbol": row.symbol, "quantity": _number(row.quantity),
        "average_cost": _money(row.average_cost), "cost_basis": _money(row.cost_basis),
        "price": _money(row.price), "price_as_of": row.price_as_of,
        "market_value": _money(row.market_value), "weight": _percent(row.nav_weight),
        "unrealized_pnl": _money(row.unrealized_pnl),
        "unrealized_pnl_percent": _percent(row.unrealized_pnl_percent),
        "account_count": len(row.accounts),
        "accounts": [{"name": account.name, "quantity": _number(account.quantity),
                      "average_cost": _money(account.average_cost)} for account in row.accounts],
    } for row in metrics.positions]
    duplicates = [row for row in position_rows if row["account_count"] > 1]
    return {
        "snapshot_id": snapshot.snapshot_id, "portfolio_as_of": snapshot.as_of,
        "market_cutoff": cutoff, "valuation_status": metrics.valuation_status,
        "provider_error": provider_error,
        "missing_price_symbol_count": len(metrics.missing_price_symbols),
        "missing_price_symbols": metrics.missing_price_symbols,
        "cards": [
            {"label": "持仓成本", "value": _money(metrics.total_cost), "note": "按导入成本确定性汇总"},
            {"label": "现金", "value": _money(metrics.cash), "note": "跨全部账户汇总"},
            {"label": "已定价权益", "value": _money(metrics.priced_equity)
             if metrics.valuation_status != "unavailable" else "未知",
             "note": f"{len(observations)} / {len(metrics.positions)} 个标的有有效行情"},
            {"label": "未实现盈亏", "value": _money(metrics.unrealized_pnl),
             "note": "仅覆盖已有有效行情的标的"},
        ],
        "positions": position_rows, "duplicate_positions": duplicates,
        "allocation": {
            "basis": metrics.allocation_basis,
            "symbols": symbol_allocation, "symbols_gradient": symbol_gradient,
            "sectors": sectors, "sectors_gradient": sector_gradient,
            "themes": themes, "themes_gradient": theme_gradient,
        },
    }


def run_metrics(record: RunRecord) -> dict:
    """Format existing Decimal results; never price or recompute a portfolio here."""
    calculation = next((item for item in record.answer.evidence
                        if item.evidence_type == "calculation"
                        and (item.structured_data or {}).get("calculation") == "portfolio_concentration"),
                       None) if record.answer else None
    if calculation is None:
        return {"cards": [], "positions": []}
    data = calculation.structured_data
    result = data["result"]

    partial = bool(result["missing_price_positions"])
    top = result["top_holding"]
    return {
        "snapshot_id": data["snapshot_id"],
        "evidence_id": calculation.evidence_id,
        "portfolio_as_of": data["portfolio_as_of"],
        "market_cutoff": data["market_cutoff"],
        "market_observation_as_of": data["market_observation_as_of"],
        "partial": partial,
        "cards": [
            {"label": "已定价净值" if partial else "组合净值", "value": _money(Decimal(result["nav"])),
             "note": "已定价权益 + 现金 · 美元"},
            {"label": "现金", "value": _money(Decimal(result["cash"])), "note": "来自本次分析的持仓快照"},
            {"label": "最大已定价持仓", "value": _percent(Decimal(top["nav_weight"])) if top else "未知",
             "note": f"{top['symbol']} · 占组合净值" if top else "没有可用价格"},
            {"label": "集中度 HHI", "value": f"{Decimal(result['hhi']):,.2f}" if top else "未知",
             "note": "按已定价权益计算 · 越高越集中"},
        ],
        "positions": [
            {"position_id": row["position_id"], "symbol": row["symbol"],
             "value": _money(Decimal(row["value"])) if row["value"] is not None else "未知",
             "weight": _percent(Decimal(row["nav_weight"])) if row["nav_weight"] is not None else "未知",
             "pnl": _money(Decimal(row["unrealized_pnl"])) if row["unrealized_pnl"] is not None else "未知"}
            for row in result["positions"]
        ],
    }
