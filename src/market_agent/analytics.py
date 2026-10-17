"""Deterministic Decimal finance calculations over normalized observations."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .domain import PortfolioSnapshot, PriceObservation


class PositionMetric(BaseModel):
    model_config = ConfigDict(frozen=True)
    position_id: str
    symbol: str
    value: Decimal | None
    nav_weight: Decimal | None
    unrealized_pnl: Decimal | None


class ConcentrationMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)
    cash: Decimal
    priced_equity: Decimal
    nav: Decimal
    priced_cost: Decimal
    unrealized_pnl: Decimal
    top_holding: PositionMetric | None
    top_three_nav_weight: Decimal
    hhi: Decimal
    sector_weights: dict[str, Decimal]
    theme_weights: dict[str, Decimal]
    positions: list[PositionMetric]
    missing_price_positions: list[str]


class AccountPositionMetric(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    quantity: Decimal
    average_cost: Decimal


class SymbolOverviewMetric(BaseModel):
    model_config = ConfigDict(frozen=True)
    symbol: str
    quantity: Decimal
    average_cost: Decimal
    cost_basis: Decimal
    price: Decimal | None
    price_as_of: datetime | None
    market_value: Decimal | None
    nav_weight: Decimal | None
    unrealized_pnl: Decimal | None
    unrealized_pnl_percent: Decimal | None
    accounts: list[AccountPositionMetric]


class PortfolioOverviewMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)
    total_cost: Decimal
    cash: Decimal
    priced_equity: Decimal
    priced_cost: Decimal
    unrealized_pnl: Decimal | None
    valuation_status: Literal["complete", "partial", "unavailable"]
    missing_price_symbols: list[str]
    allocation_basis: Literal["market", "cost"]
    symbol_allocation: dict[str, Decimal]
    sector_allocation: dict[str, Decimal]
    theme_allocation: dict[str, Decimal]
    positions: list[SymbolOverviewMetric]


def fresh_observations(observations: list[PriceObservation], cutoff: datetime,
                       max_age: timedelta) -> tuple[list[PriceObservation], list[str]]:
    stale = sorted({item.symbol for item in observations
                    if item.as_of > cutoff or cutoff - item.as_of > max_age})
    return ([item for item in observations if item.symbol not in stale], stale)


def _weights(values: dict[str, Decimal], denominator: Decimal) -> dict[str, Decimal]:
    return ({key: value / denominator for key, value in values.items()}
            if denominator else {})


def _validated_price_map(snapshot: PortfolioSnapshot,
                         prices: list[PriceObservation]) -> dict[str, PriceObservation]:
    by_symbol = {observation.symbol: observation for observation in prices}
    if len(by_symbol) != len(prices):
        raise ValueError("duplicate normalized price for symbol")
    if any(observation.currency != snapshot.base_currency for observation in prices):
        raise ValueError("FX conversion is outside this slice")
    return by_symbol


def compute_portfolio_overview(snapshot: PortfolioSnapshot,
                               prices: list[PriceObservation]) -> PortfolioOverviewMetrics:
    """Aggregate the immutable snapshot by symbol using authoritative Decimal arithmetic."""
    concentration = compute_analytics(snapshot, prices)
    by_symbol = {observation.symbol: observation for observation in prices}
    account_names = {account.account_id: account.name for account in snapshot.accounts}
    grouped: dict[str, dict] = {}
    for position in snapshot.positions:
        row = grouped.setdefault(position.symbol, {
            "quantity": Decimal(0), "cost": Decimal(0), "accounts": [],
        })
        row["quantity"] += position.quantity
        row["cost"] += position.quantity * position.avg_cost
        row["accounts"].append(AccountPositionMetric(
            name=account_names[position.account_id], quantity=position.quantity,
            average_cost=position.avg_cost,
        ))
    for row in grouped.values():
        row["accounts"].sort(key=lambda account: account.name)

    cash = concentration.cash
    total_cost = sum((row["cost"] for row in grouped.values()), Decimal(0))
    missing = sorted(set(grouped) - set(by_symbol))
    status = "complete" if not missing else "partial" if by_symbol else "unavailable"
    market_values: dict[str, Decimal] = {}
    cost_values = {symbol: row["cost"] for symbol, row in grouped.items()}
    cost_sectors: dict[str, Decimal] = {}
    cost_themes: dict[str, Decimal] = {}
    for symbol, row in grouped.items():
        override = snapshot.instrument_overrides.get(symbol)
        sector = override.sector if override and override.sector else "待分类"
        theme = override.theme if override and override.theme else "待分类"
        cost_sectors[sector] = cost_sectors.get(sector, Decimal(0)) + row["cost"]
        cost_themes[theme] = cost_themes.get(theme, Decimal(0)) + row["cost"]
        observation = by_symbol.get(symbol)
        if observation:
            value = row["quantity"] * observation.price
            market_values[symbol] = value
    priced_equity = concentration.priced_equity
    priced_cost = concentration.priced_cost
    nav = priced_equity + cash
    position_metrics = []
    for symbol, row in grouped.items():
        observation = by_symbol.get(symbol)
        value = market_values.get(symbol)
        pnl = value - row["cost"] if value is not None else None
        position_metrics.append(SymbolOverviewMetric(
            symbol=symbol, quantity=row["quantity"], average_cost=row["cost"] / row["quantity"],
            cost_basis=row["cost"], price=observation.price if observation else None,
            price_as_of=observation.as_of if observation else None, market_value=value,
            nav_weight=value / nav if status == "complete" and value is not None and nav else None,
            unrealized_pnl=pnl,
            unrealized_pnl_percent=pnl / row["cost"] if pnl is not None and row["cost"] else None,
            accounts=row["accounts"],
        ))
    position_metrics.sort(key=lambda row: (-(row.market_value if row.market_value is not None
                                             else row.cost_basis), row.symbol))
    if status == "complete":
        symbol_values = {**market_values, **({"现金": cash} if cash else {})}
        symbol_allocation = _weights(symbol_values, nav)
        sector_allocation = concentration.sector_weights
        theme_allocation = concentration.theme_weights
        basis = "market"
    else:
        cost_nav = total_cost + cash
        symbol_values = {**cost_values, **({"现金": cash} if cash else {})}
        symbol_allocation = _weights(symbol_values, cost_nav)
        sector_allocation = _weights(cost_sectors, total_cost)
        theme_allocation = _weights(cost_themes, total_cost)
        basis = "cost"
    return PortfolioOverviewMetrics(
        total_cost=total_cost, cash=cash, priced_equity=priced_equity,
        priced_cost=priced_cost,
        unrealized_pnl=concentration.unrealized_pnl if by_symbol else None,
        valuation_status=status, missing_price_symbols=missing, allocation_basis=basis,
        symbol_allocation=symbol_allocation, sector_allocation=sector_allocation,
        theme_allocation=theme_allocation, positions=position_metrics,
    )


def compute_analytics(snapshot: PortfolioSnapshot, prices: list[PriceObservation]) -> ConcentrationMetrics:
    by_symbol = _validated_price_map(snapshot, prices)
    cash = sum((account.cash for account in snapshot.accounts), Decimal(0))
    valued = []
    missing = []
    for position in snapshot.positions:
        observation = by_symbol.get(position.symbol)
        if observation is None:
            missing.append(position.position_id)
            continue
        value = position.quantity * observation.price
        cost = position.quantity * position.avg_cost
        valued.append((position, value, cost))
    equity = sum((row[1] for row in valued), Decimal(0))
    nav = equity + cash
    priced_cost = sum((row[2] for row in valued), Decimal(0))
    pnl = equity - priced_cost
    metrics = [PositionMetric(
        position_id=position.position_id, symbol=position.symbol, value=value,
        nav_weight=value / nav if nav else None, unrealized_pnl=value - cost,
    ) for position, value, cost in valued]
    metrics.extend(PositionMetric(position_id=position.position_id, symbol=position.symbol,
                                  value=None, nav_weight=None, unrealized_pnl=None)
                   for position in snapshot.positions if position.position_id in missing)
    # Capital Desk groups the same symbol across accounts before concentration ranking.
    merged: dict[str, tuple[Decimal, Decimal, list[str]]] = {}
    for position, value, cost in valued:
        old_value, old_cost, ids = merged.get(position.symbol, (Decimal(0), Decimal(0), []))
        merged[position.symbol] = (old_value + value, old_cost + cost, ids + [position.position_id])
    ranked = sorted((PositionMetric(
        position_id=ids[0] if len(ids) == 1 else symbol,
        symbol=symbol, value=value, nav_weight=value / nav if nav else None,
        unrealized_pnl=value - cost,
    ) for symbol, (value, cost, ids) in merged.items()),
                    key=lambda item: (-item.value, item.symbol))
    top_three = sum((item.nav_weight for item in ranked[:3]), Decimal(0))
    hhi = sum(((item.value / equity) ** 2 for item in ranked), Decimal(0)) * Decimal(10000) if equity else Decimal(0)
    sectors: dict[str, Decimal] = {}
    themes: dict[str, Decimal] = {}
    for position, value, _ in valued:
        override = snapshot.instrument_overrides.get(position.symbol)
        sector = override.sector if override and override.sector else "待分类"
        theme = override.theme if override and override.theme else "待分类"
        sectors[sector] = sectors.get(sector, Decimal(0)) + value
        themes[theme] = themes.get(theme, Decimal(0)) + value
    if equity:
        sectors = {key: value / equity for key, value in sectors.items()}
        themes = {key: value / equity for key, value in themes.items()}
    return ConcentrationMetrics(cash=cash, priced_equity=equity, nav=nav, priced_cost=priced_cost,
                                unrealized_pnl=pnl, top_holding=ranked[0] if ranked else None,
                                top_three_nav_weight=top_three, hhi=hhi, sector_weights=sectors,
                                theme_weights=themes, positions=metrics, missing_price_positions=missing)
