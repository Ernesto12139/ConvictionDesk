"""Finnhub company-news adapter with normalized, point-in-time evidence."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict

from .clock import Clock, system_utc_now
from .domain import EvidenceItem, utc
from .provider import ProviderError, SYMBOL


FINNHUB_BASE_URL = "https://finnhub.io/api/v1"


class NewsResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence: list[EvidenceItem]
    # None preserves the older provider contract. The Finnhub adapter supplies
    # an explicit verdict after its bounded date-window coverage check.
    coverage_complete: bool | None = None


class NewsProvider(Protocol):
    name: str

    def search(
        self,
        symbols: list[str],
        since: datetime,
        until: datetime,
        limit: int,
    ) -> NewsResult: ...


def _content_hash(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class _Receipts(list):
    """Time-filtered receipts plus how many records the provider returned."""

    received = 0


class FinnhubNewsProvider:
    """Retrieve North American company news through Finnhub's company-news endpoint."""

    name = "finnhub"

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        clock: Clock | None = None,
        base_url: str = FINNHUB_BASE_URL,
    ):
        self._api_key = api_key
        self.client = client or httpx.Client(timeout=15)
        self.clock = clock or system_utc_now
        self.base_url = base_url.rstrip("/")

    def search(
        self,
        symbols: list[str],
        since: datetime,
        until: datetime,
        limit: int = 20,
    ) -> NewsResult:
        try:
            start = utc(since)
            cutoff = utc(until)
        except (TypeError, ValueError) as exc:
            raise ProviderError("invalid_input", "news timestamps must be timezone-aware") from exc
        if start > cutoff:
            raise ProviderError("invalid_input", "news since must not exceed until")
        if not 1 <= limit <= 100:
            raise ProviderError("invalid_input", "news limit must be between 1 and 100")
        if not symbols or any(
            not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol) for symbol in symbols
        ):
            raise ProviderError("invalid_input", "invalid US equity symbol")
        if not self._api_key:
            raise ProviderError("configuration", "FINNHUB_API_KEY is not configured")

        def saturated(window: list[EvidenceItem]) -> bool:
            # Judge truncation by what Finnhub returned, not what survived the
            # exact-time filter: out-of-range records can occupy capped slots.
            return max(len(window), getattr(window, "received", len(window))) >= limit

        items: dict[str, EvidenceItem] = {}
        coverage_complete = True
        expanded = False
        for symbol in sorted(set(symbols)):
            first = self._one(symbol, start, cutoff)
            windows = [first]
            if limit == 100 and len(set(symbols)) == 1 and saturated(first):
                # Finnhub has no offset cursor here. Split a saturated range by
                # whole publication dates; at most two extra requests per symbol.
                # A saturated half remains honestly partial, including a busy
                # single day that cannot be paged through this endpoint.
                span = (cutoff.date()-start.date()).days
                if span:
                    split = start.date()+timedelta(days=span//2)
                    newer_start = datetime.combine(split+timedelta(days=1), datetime.min.time(), timezone.utc)
                    # Overlap the halves by a day on each side of the boundary so
                    # a provider reading dates outside UTC cannot drop records
                    # near midnight; receipts are deduplicated by evidence ID.
                    older_end = min(cutoff, newer_start+timedelta(days=1, microseconds=-1))
                    newer_begin = max(start, newer_start-timedelta(days=1))
                    windows = [self._one(symbol, start, older_end),
                               self._one(symbol, newer_begin, cutoff)]
                    expanded = True
                else:
                    coverage_complete = False
            if any(saturated(window) for window in windows):
                coverage_complete = False
            for item in (item for window in windows for item in window[:limit]):
                items.setdefault(item.evidence_id, item)
        selected = sorted(
            items.values(),
            key=lambda item: (item.published_at, item.evidence_id),
            reverse=True,
        )
        if not expanded:
            if len(selected) > limit:
                coverage_complete = False
            selected = selected[:limit]
        return NewsResult(evidence=selected, coverage_complete=coverage_complete)

    def _one(self, symbol: str, since: datetime, until: datetime) -> list[EvidenceItem]:
        try:
            response = self.client.get(
                f"{self.base_url}/company-news",
                headers={"X-Finnhub-Token": self._api_key},
                params={
                    "symbol": symbol,
                    "from": since.date().isoformat(),
                    "to": until.date().isoformat(),
                },
            )
        except httpx.TimeoutException:
            raise ProviderError("timeout", "Finnhub request timed out") from None
        except httpx.RequestError:
            raise ProviderError("transport", "Finnhub transport failure") from None

        if response.status_code in (400, 422):
            raise ProviderError("invalid_input", f"Finnhub rejected request ({response.status_code})")
        if response.status_code in (401, 403):
            raise ProviderError("authentication", f"Finnhub authentication failed ({response.status_code})")
        if response.status_code == 429:
            raise ProviderError("rate_limit", "Finnhub rate limit exceeded (429)")
        if response.status_code >= 500:
            raise ProviderError("retryable", f"Finnhub unavailable ({response.status_code})")
        if response.status_code != 200:
            raise ProviderError("provider", f"Finnhub request failed ({response.status_code})")

        try:
            raw = response.json()
            if not isinstance(raw, list):
                raise ValueError("company news response must be an array")
            retrieved_at = utc(self.clock())
            normalized = [
                self._normalize(record, symbol, retrieved_at)
                for record in raw
            ]
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProviderError("invalid_data", "Finnhub returned invalid company news data") from exc
        kept = _Receipts(
            item for item in normalized
            if item is not None and since <= item.published_at <= until
        )
        kept.received = len(raw)
        return kept

    def _normalize(
        self, record: object, symbol: str, retrieved_at: datetime
    ) -> EvidenceItem | None:
        if not isinstance(record, dict):
            raise ValueError("news item must be an object")
        timestamp = record.get("datetime")
        provider_id = record.get("id")
        headline = record.get("headline")
        source = record.get("source")
        summary = record.get("summary")
        uri = record.get("url")
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError("invalid news timestamp")
        if not isinstance(provider_id, (str, int)) or isinstance(provider_id, bool):
            raise ValueError("invalid news ID")
        if not isinstance(headline, str) or not headline.strip():
            raise ValueError("invalid news headline")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("invalid news source")
        if summary is not None and not isinstance(summary, str):
            raise ValueError("invalid news summary")
        if uri is not None and not isinstance(uri, str):
            raise ValueError("invalid news URL")
        published_at = datetime.fromtimestamp(timestamp, timezone.utc)
        payload = {
            "symbol": symbol,
            "headline": headline.strip(),
            "summary": summary.strip() if summary else None,
            "source": source.strip(),
            "source_uri": uri or None,
            "source_record_id": str(provider_id),
            "published_at": published_at.isoformat(),
        }
        digest = _content_hash(payload)
        evidence_id = "E" + digest[7:23]
        return EvidenceItem(
            evidence_id=evidence_id,
            evidence_type="news",
            provider=self.name,
            symbol=symbol,
            title=payload["headline"],
            source_uri=payload["source_uri"],
            source_record_id=payload["source_record_id"],
            published_at=published_at,
            retrieved_at=retrieved_at,
            content_hash=digest,
            excerpt=payload["summary"],
            structured_data={"source": payload["source"]},
        )
