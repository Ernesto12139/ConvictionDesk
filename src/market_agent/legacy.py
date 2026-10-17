"""Validation and one-way migration of Capital Desk JSON version 1."""

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from .clock import system_utc_now
from .domain import (Account, InstrumentOverride, PortfolioContent, PortfolioSnapshot, Position,
                     SourceMetadata, portfolio_content_hash, utc)


SYMBOL = re.compile(r"^[A-Z][A-Z0-9.^/\-]{0,14}$")


def _decimal(value: object, label: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} must be a finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not number.is_finite() or (number <= 0 if positive else number < 0):
        raise ValueError(f"{label} must be {'positive' if positive else 'nonnegative'}")
    return number


def _symbol(value: object) -> str:
    symbol = str(value or "").strip().upper()
    if not SYMBOL.fullmatch(symbol):
        raise ValueError(f"invalid symbol: {value!r}")
    return symbol


def _hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def migrate_capital_desk_v1(raw: dict, *, imported_at: datetime | None = None) -> PortfolioSnapshot:
    """Return a new immutable snapshot; never modify the caller's object."""
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        raise ValueError("Capital Desk version must be 1")
    accounts_raw = raw.get("accounts")
    holdings_raw = raw.get("holdings")
    assets_raw = raw.get("assets")
    watchlist_raw = raw.get("watchlist")
    if not isinstance(accounts_raw, list) or len(accounts_raw) != 4:
        raise ValueError("Capital Desk v1 requires exactly four accounts")
    if not isinstance(holdings_raw, list) or len(holdings_raw) > 5000:
        raise ValueError("holdings must be a list of at most 5000")
    if not isinstance(assets_raw, dict):
        raise ValueError("assets must be an object")
    if not isinstance(watchlist_raw, list) or len(watchlist_raw) > 500:
        raise ValueError("watchlist must be a list of at most 500")

    accounts = []
    account_ids = set()
    names = set()
    for item in accounts_raw:
        if not isinstance(item, dict):
            raise ValueError("account must be an object")
        account_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not account_id or account_id in account_ids or not name or name in names:
            raise ValueError("account IDs and names must be nonempty and unique")
        account_ids.add(account_id)
        names.add(name)
        accounts.append(Account(account_id=account_id, name=name, cash=_decimal(item.get("cash"), "cash")))

    positions = []
    seen_keys = set()
    seen_ids = set()
    for item in holdings_raw:
        if not isinstance(item, dict):
            raise ValueError("holding must be an object")
        account_id = str(item.get("accountId") or "").strip()
        symbol = _symbol(item.get("symbol"))
        if account_id not in account_ids:
            raise ValueError("holding refers to unknown account")
        key = (account_id, symbol)
        if key in seen_keys:
            raise ValueError("duplicate holding in one account")
        seen_keys.add(key)
        legacy_id = str(item.get("id") or "").strip()
        if legacy_id:
            if legacy_id in seen_ids:
                raise ValueError("duplicate holding ID")
            seen_ids.add(legacy_id)
        decision = str(item.get("decision") or "hold").lower()
        if decision not in {"add", "hold", "reduce"}:
            decision = "hold"
        positions.append(Position(
            position_id=f"{account_id}:{symbol}", account_id=account_id, symbol=symbol,
            quantity=_decimal(item.get("shares"), "shares", positive=True),
            avg_cost=_decimal(item.get("avgCost"), "avgCost"),
            user_stage=decision, note=str(item.get("note") or "")[:500],
        ))

    overrides = {}
    manual_prices = {}
    for key, item in assets_raw.items():
        symbol = _symbol(key)
        if symbol in overrides or not isinstance(item, dict):
            raise ValueError("invalid or duplicate asset")
        overrides[symbol] = InstrumentOverride(
            name=str(item.get("name") or symbol),
            sector=str(item.get("sector") or "待分类"),
            theme=str(item.get("theme") or "待分类"),
        )
        price = item.get("price")
        if price is not None and price != "":
            manual_prices[symbol] = {"price": str(_decimal(price, "asset price", positive=True)),
                                     "priceAsOf": item.get("priceAsOf")}

    watchlist = list(dict.fromkeys(_symbol(value) for value in watchlist_raw))
    timestamp = utc(imported_at if imported_at is not None else system_utc_now())
    source = SourceMetadata(kind="capital_desk_v1_import", imported_at=timestamp,
                            source_hash=_hash(raw), legacy_manual_prices=manual_prices)
    content = PortfolioContent(as_of=timestamp, accounts=accounts, positions=positions,
                               watchlist=watchlist, instrument_overrides=overrides, source=source)
    content_hash = portfolio_content_hash(content)
    return PortfolioSnapshot(**content.model_dump(), snapshot_id=str(uuid4()), content_hash=content_hash)
