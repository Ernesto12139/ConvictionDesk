"""Optional current-only USD US-equity reference; EOD remains market truth."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol

import httpx

from .clock import system_utc_now
from .decision_context import MarketReference, retrieve_reference
from .domain import EvidenceItem, utc
from .provider import SYMBOL, ProviderError
from .tdb import content_hash


class CurrentQuoteProvider(Protocol):
    def reference(self, symbol: str, now: datetime) -> tuple[MarketReference | None, EvidenceItem | None, str]: ...


class FinnhubQuoteProvider:
    """Official /quote c and t, fetched on demand, never candles or streaming.

    US-equity scope of this endpoint supplies USD prices. t is the provider quote
    timestamp in Unix seconds; it is not relabelled as a trade or market-open time.
    """
    name = "finnhub"

    def __init__(self, api_key, *, client=None, clock=system_utc_now):
        self._api_key = api_key
        self.client = client or httpx.Client(timeout=10)
        self.clock = clock

    def reference(self, symbol, now):
        now = utc(now)
        if not SYMBOL.fullmatch(symbol):
            return None, None, "INVALID_INPUT"
        if not self._api_key:
            return None, None, "UNAVAILABLE"
        response = None
        for attempt in range(2):
            try:
                from .tool_deadline import check_tool_deadline, provider_get
                response = provider_get(self.client, "https://finnhub.io/api/v1/quote",
                    params={"symbol": symbol}, headers={"X-Finnhub-Token": self._api_key})
                check_tool_deadline()
            except httpx.RequestError:
                if attempt == 0:
                    continue
                return None, None, "UNAVAILABLE"
            if response.status_code >= 500 and attempt == 0:
                continue
            break
        if response.status_code in {401, 403}:
            return None, None, "NOT_ENTITLED"
        if response.status_code != 200:
            return None, None, "UNAVAILABLE"
        try:
            raw = json.loads(response.text, parse_float=Decimal)
            if not isinstance(raw, dict) or type(raw.get("t")) is not int or raw["t"] <= 0:
                return None, None, "NO_TIMESTAMPED_PRICE"
            if isinstance(raw.get("c"), bool):
                return None, None, "INVALID_DATA"
            price = Decimal(str(raw.get("c")))
            stamp = datetime.fromtimestamp(raw["t"], timezone.utc)
            retrieved = utc(self.clock())
            if not price.is_finite() or price <= 0 or stamp > retrieved:
                return None, None, "INVALID_DATA"
            if retrieved - stamp > timedelta(days=7):
                return None, None, "STALE"
        except (ValueError, TypeError, OverflowError, OSError, InvalidOperation):
            return None, None, "INVALID_DATA"
        payload = {"symbol": symbol, "price": str(price), "currency": "USD", "as_of": stamp.isoformat(),
                   "provider": self.name, "basis": "QUOTE_CURRENT", "session": "UNKNOWN"}
        digest = content_hash(payload)
        eid = "E" + digest[7:23]
        reference = MarketReference(price=price, as_of=stamp, retrieved_at=retrieved,
            provider=self.name, evidence_id=eid, basis="QUOTE_CURRENT", session="UNKNOWN")
        item = EvidenceItem(evidence_id=eid, evidence_type="market_data", provider=self.name,
            symbol=symbol, title="Finnhub 最新可用报价参考 · 可能延迟", source_uri="https://finnhub.io/docs/api/quote",
            source_record_id=f"quote:{symbol}:{raw['t']}", as_of=stamp, retrieved_at=retrieved,
            content_hash=digest, structured_data=payload)
        return reference, item, "AVAILABLE"


def current_reference(market, quote_provider, symbol, now):
    """Massive first, existing Finnhub capability second; caller owns current gate."""
    try:
        result = retrieve_reference(market, symbol, now)
    except ProviderError as exc:
        result = (None, None, exc.code)
    if result[0] is not None or quote_provider is None:
        return result
    return quote_provider.reference(symbol, now)
