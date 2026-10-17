"""Normalized EOD retrieval, provider failover, and source-owned evidence."""

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from .clock import Clock, system_utc_now
from .domain import EvidenceItem, PriceObservation, utc
from .persistence import Store


SYMBOL = re.compile(r"^[A-Z][A-Z0-9.^/\-]{0,14}$")
EASTERN = ZoneInfo("America/New_York")
MASSIVE_BASE_URL = "https://api.massive.com"
CACHE_TTL = timedelta(hours=1)
MAX_EOD_AGE = timedelta(days=7)


def _hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _validate_request(symbols: list[str], as_of: datetime) -> datetime:
    try:
        cutoff = utc(as_of)
    except (TypeError, ValueError) as exc:
        raise ProviderError("invalid_input", "as_of must be timezone-aware") from exc
    if any(not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol) for symbol in symbols):
        raise ProviderError("invalid_input", "invalid US equity symbol")
    return cutoff


class ProviderError(Exception):
    """A provider failure without response bodies, request headers, or credentials."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    @property
    def semantic(self) -> bool:
        return self.code == "invalid_input"


class MarketDataResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    observations: list[PriceObservation]
    evidence: list[EvidenceItem]

    @model_validator(mode="after")
    def matching_evidence(self):
        by_id = {item.evidence_id: item for item in self.evidence}
        if len(by_id) != len(self.evidence) or set(by_id) != {
            observation.evidence_id for observation in self.observations
        }:
            raise ValueError("market observations and evidence must match")
        if any(by_id[observation.evidence_id].provider != observation.provider
               or by_id[observation.evidence_id].symbol != observation.symbol
               for observation in self.observations):
            raise ValueError("market evidence provenance does not match observation")
        return self


class EODProvider(Protocol):
    name: str

    def retrieve_eod(self, symbols: list[str], as_of: datetime) -> MarketDataResult: ...


class UnconfiguredEODProvider:
    """Fail closed when the normal application has no market source."""

    name = "unconfigured"

    def retrieve_eod(self, symbols: list[str], as_of: datetime) -> MarketDataResult:
        _validate_request(symbols, as_of)
        raise ProviderError("configuration", "No market EOD provider is configured")


class FixtureMarketProvider:
    name = "fixture_eod"

    def __init__(self, path: str | Path, *, clock: Clock | None = None):
        records = json.loads(Path(path).read_text(encoding="utf-8"))
        self.records = [PriceObservation(**record, evidence_id=self.evidence_id(record)) for record in records]
        self.clock = clock or system_utc_now

    @staticmethod
    def evidence_id(record: dict) -> str:
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "E" + hashlib.sha256(payload.encode()).hexdigest()[:16]

    def get_eod(self, symbols: list[str], as_of: datetime) -> list[PriceObservation]:
        cutoff = _validate_request(symbols, as_of)
        selected = {}
        for record in self.records:
            if record.symbol in symbols and record.as_of <= cutoff:
                current = selected.get(record.symbol)
                if current is None or record.as_of > current.as_of:
                    selected[record.symbol] = record
        return [selected[symbol] for symbol in sorted(selected)]

    def retrieve_eod(self, symbols: list[str], as_of: datetime) -> MarketDataResult:
        observations = self.get_eod(symbols, as_of)
        retrieved_at = utc(self.clock())
        evidence = []
        for observation in observations:
            payload = observation.model_dump(mode="json")
            evidence.append(EvidenceItem(
                evidence_id=observation.evidence_id, evidence_type="market_data",
                provider=observation.provider, symbol=observation.symbol,
                source_record_id=observation.evidence_id, retrieved_at=retrieved_at,
                as_of=observation.as_of, content_hash=_hash(payload), structured_data=payload,
            ))
        return MarketDataResult(observations=observations, evidence=evidence)


class MassiveEODProvider:
    """Retrieve unadjusted completed daily bars from Massive's custom aggregates API."""

    name = "massive"

    def __init__(self, api_key: str, store: Store, *, client: httpx.Client | None = None,
                 clock: Clock | None = None, base_url: str = MASSIVE_BASE_URL):
        self.api_key = api_key
        self.store = store
        self.client = client or httpx.Client(timeout=10)
        self.clock = clock or system_utc_now
        self.base_url = base_url.rstrip("/")

    def completed_history(self, symbol: str, as_of: datetime):
        """Current descriptive split-adjusted closes, not historical vintages.

        Conservative whole-Eastern-day gate includes extended sessions. No
        exchange calendar, dividend adjustment, gap filling or local split repair.
        A single frozen series evidence binds every normalized bar and basis.
        """
        from .investment_context import CloseHistory, CompletedClose, completed_session_end, digest
        cutoff = _validate_request([symbol], as_of)
        retrieved_at = utc(self.clock())
        if not self.api_key:
            raise ProviderError("configuration", "MASSIVE_API_KEY is not configured")
        if not timedelta(0) <= retrieved_at-cutoff <= timedelta(minutes=5):
            raise ProviderError("historical_vintage_unavailable", "Current history cannot prove historical vintage")
        last = cutoff.astimezone(EASTERN).date()-timedelta(days=1)
        first = last-timedelta(days=550)
        url = self.base_url+f"/v2/aggs/ticker/{quote(symbol, safe='')}/range/1/day/{first}/{last}"
        try:
            response = self.client.get(url, headers={"Authorization": "Bearer "+self.api_key},
                params={"adjusted":"true", "sort":"asc", "limit":50000})
        except httpx.RequestError as exc:
            raise ProviderError("timeout" if isinstance(exc,httpx.TimeoutException) else "transport",
                "Massive history transport failure") from None
        if response.status_code in (401,403):
            raise ProviderError("not_entitled", "Completed history unavailable under current credentials")
        if response.status_code == 429:
            raise ProviderError("rate_limit", "Massive history rate limit")
        if response.status_code != 200:
            raise ProviderError("provider", "Massive history unavailable")
        try:
            raw = json.loads(response.text, parse_float=Decimal)
            rows = raw.get("results",[])
            if (raw.get("status")!="OK" or raw.get("ticker")!=symbol or raw.get("adjusted") is not True
                or raw.get("next_url") or not isinstance(rows,list)
                or type(raw.get("resultsCount")) is not int or raw["resultsCount"]!=len(rows)
                or len(rows)>600 or not isinstance(raw.get("request_id"),str) or not raw["request_id"]):
                raise ValueError("history envelope, basis or pagination mismatch")
            # Content-derived ID includes request lineage and exact normalized bars.
            series_id = "E"+digest({"symbol":symbol,"request_id":raw["request_id"],
                "rows":[{"t":b["t"],"c":str(b["c"])} for b in rows]})[7:23]
            bars=[]
            for row in rows:
                if type(row["t"]) is not int or isinstance(row["c"],bool):
                    raise ValueError("invalid history timestamp/close")
                timestamp = datetime.fromtimestamp(row["t"]/1000,timezone.utc).astimezone(EASTERN)
                day = timestamp.date()
                if timestamp.time()!=datetime.min.time() or not first<=day<=last or day.weekday()>=5:
                    raise ValueError("not a completed requested daily session")
                bars.append(CompletedClose(session_date=day,available_at=completed_session_end(day),
                    close=Decimal(str(row["c"])),evidence_id=series_id))
            if len({b.session_date for b in bars})!=len(bars):
                raise ValueError("duplicate history session")
            history = CloseHistory(symbol=symbol,price_basis="SPLIT_ADJUSTED",
                corporate_actions_verified=True,completeness="PROVIDED_SESSIONS",
                source="massive_custom_daily_split_adjusted",bars=tuple(bars),source_evidence_ids=(series_id,))
            payload = history.model_dump(mode="json")
            item = EvidenceItem(evidence_id=series_id,evidence_type="market_data",provider=self.name,
                symbol=symbol,title="Massive split-adjusted completed daily closes",
                source_uri=url+"?adjusted=true&sort=asc&limit=50000",
                source_record_id=raw["request_id"],retrieved_at=retrieved_at,
                as_of=max((b.available_at for b in bars),default=None),content_hash=digest(payload),
                structured_data=payload,
                excerpt="拆股调整收盘价；排除当前纽约日期。仅当前描述性结构，不含股息总回报，不保证交易所日历完整或历史数据版本。")
            return history,[item]
        except (ValueError,TypeError,KeyError,AttributeError,OverflowError,OSError,InvalidOperation) as exc:
            raise ProviderError("invalid_data", "Massive history failed basis/session/lineage validation") from None

    def retrieve_eod(self, symbols: list[str], as_of: datetime) -> MarketDataResult:
        cutoff = _validate_request(symbols, as_of)
        if not self.api_key:
            raise ProviderError("configuration", "MASSIVE_API_KEY is not configured")
        observations: list[PriceObservation] = []
        evidence: list[EvidenceItem] = []
        for symbol in sorted(set(symbols)):
            result = self._one(symbol, cutoff)
            observations.extend(result.observations)
            evidence.extend(result.evidence)
        return MarketDataResult(observations=observations, evidence=evidence)

    def _one(self, symbol: str, cutoff: datetime) -> MarketDataResult:
        retrieved_at = utc(self.clock())
        cached = self.store.get_provider_cache(self.name, symbol, cutoff)
        if cached is not None:
            cached_at, result = cached
            if (timedelta(0) <= retrieved_at - cached_at <= CACHE_TTL
                    and all(item.as_of <= cutoff and item.symbol == symbol and item.provider == self.name
                            for item in result.observations)):
                return result

        # Daily bars become eligible only after their full Eastern Time date.
        last_date = cutoff.astimezone(EASTERN).date() - timedelta(days=1)
        first_date = last_date - timedelta(days=30)
        path = f"/v2/aggs/ticker/{quote(symbol, safe='')}/range/1/day/{first_date.isoformat()}/{last_date.isoformat()}"
        url = self.base_url + path
        try:
            response = self.client.get(url, headers={"Authorization": f"Bearer {self.api_key}"},
                                       params={"adjusted": "false", "sort": "desc", "limit": 50})
        except httpx.RequestError as exc:
            raise ProviderError("transport", f"Massive transport failure: {type(exc).__name__}") from exc
        if response.status_code in (400, 422):
            raise ProviderError("invalid_input", f"Massive rejected request ({response.status_code})")
        if response.status_code == 429:
            raise ProviderError("rate_limit", "Massive request limit reached")
        if response.status_code >= 500:
            raise ProviderError("retryable", f"Massive unavailable ({response.status_code})")
        if response.status_code != 200:
            raise ProviderError("provider", f"Massive request failed ({response.status_code})")
        try:
            raw = json.loads(response.text, parse_float=Decimal)
            result = self._normalize(raw, symbol, cutoff, retrieved_at, url)
        except (ValueError, TypeError, KeyError, OverflowError, InvalidOperation, ValidationError) as exc:
            raise ProviderError("invalid_data", "Massive returned invalid EOD data") from exc
        if result.observations:
            self.store.put_provider_cache(self.name, symbol, cutoff, retrieved_at, result)
        return result

    def _normalize(self, raw: object, symbol: str, cutoff: datetime,
                   retrieved_at: datetime, source_uri: str) -> MarketDataResult:
        if not isinstance(raw, dict) or raw.get("status") != "OK" or raw.get("ticker") != symbol:
            raise ValueError("unexpected Massive response")
        if raw.get("adjusted") is not False or raw.get("next_url"):
            raise ValueError("adjustment or pagination mismatch")
        bars = raw.get("results", [])
        if not isinstance(bars, list):
            raise ValueError("results must be an array")
        count = raw.get("resultsCount")
        if type(count) is not int or count != len(bars):
            raise ValueError("results count mismatch")
        request_id = raw.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("missing provider request ID")
        selected: tuple[datetime, Decimal, int] | None = None
        for bar in bars:
            if not isinstance(bar, dict) or type(bar.get("t")) is not int:
                raise ValueError("invalid bar timestamp")
            timestamp = bar["t"]
            start = datetime.fromtimestamp(timestamp / 1000, timezone.utc).astimezone(EASTERN)
            if start.hour != 0 or start.minute != 0 or start.second != 0:
                raise ValueError("bar does not start at Eastern midnight")
            date_end = datetime.combine(start.date() + timedelta(days=1), datetime.min.time(), EASTERN)
            as_of = (date_end - timedelta(microseconds=1)).astimezone(timezone.utc)
            value = bar.get("c")
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
                raise ValueError("invalid close")
            price = Decimal(str(value))
            if not price.is_finite() or price <= 0:
                raise ValueError("invalid close")
            if as_of <= cutoff and (selected is None or as_of > selected[0]):
                selected = (as_of, price, timestamp)
        if selected is None:
            return MarketDataResult(observations=[], evidence=[])
        as_of, price, timestamp = selected
        payload = {"symbol": symbol, "price": str(price), "currency": "USD",
                   "as_of": as_of.isoformat(), "provider": self.name,
                   "session_date": datetime.fromtimestamp(timestamp / 1000, timezone.utc).astimezone(EASTERN).date().isoformat(),
                   "price_basis": "unadjusted"}
        source_record_id = f"{request_id}:{timestamp}"
        digest = _hash({"observation": payload, "source_record_id": source_record_id})
        evidence_id = "E" + digest[7:23]
        observation = PriceObservation(symbol=symbol, price=price, as_of=as_of,
                                       provider=self.name, evidence_id=evidence_id)
        item = EvidenceItem(evidence_id=evidence_id, evidence_type="market_data",
                            provider=self.name, symbol=symbol,
                            source_uri=source_uri + "?adjusted=false&sort=desc&limit=50",
                            source_record_id=source_record_id, retrieved_at=retrieved_at,
                            as_of=as_of, content_hash=digest, structured_data=payload)
        return MarketDataResult(observations=[observation], evidence=[item])


class MassiveGroupedEODProvider(MassiveEODProvider):
    """Use one unadjusted daily market summary for a multi-symbol portfolio."""

    def __init__(self, api_key: str, store: Store, **kwargs):
        super().__init__(api_key, store, **kwargs)
        self._grouped_cache: dict[date, tuple[datetime, dict, str]] = {}

    def retrieve_eod(self, symbols: list[str], as_of: datetime) -> MarketDataResult:
        cutoff = _validate_request(symbols, as_of)
        if not self.api_key:
            raise ProviderError("configuration", "MASSIVE_API_KEY is not configured")
        requested = set(symbols)
        if len(requested) <= 1:
            # Overview and single-holding TDB share a provider and source lineage.
            now = utc(self.clock())
            for market_date, (retrieved_at, body, url) in sorted(self._grouped_cache.items(), reverse=True):
                session_end = (datetime.combine(market_date + timedelta(days=1),
                                                datetime.min.time(), EASTERN)
                               - timedelta(microseconds=1)).astimezone(timezone.utc)
                if (not timedelta(0) <= now-retrieved_at <= CACHE_TTL
                        or not timedelta(0) <= cutoff-session_end <= MAX_EOD_AGE):
                    continue
                result = self._normalize_grouped(body, requested, market_date, cutoff, retrieved_at, url)
                if result.observations:
                    return result
            return super().retrieve_eod(symbols, cutoff)
        # Exclude the current Eastern date, then ask only weekday candidates
        # whose completed daily bar can still pass the shared freshness rule.
        latest_date = cutoff.astimezone(EASTERN).date() - timedelta(days=1)
        for days_back in range(MAX_EOD_AGE.days + 1):
            market_date = latest_date - timedelta(days=days_back)
            if market_date.weekday() >= 5:
                continue
            session_end = (datetime.combine(market_date + timedelta(days=1),
                                            datetime.min.time(), EASTERN)
                           - timedelta(microseconds=1)).astimezone(timezone.utc)
            if cutoff - session_end > MAX_EOD_AGE:
                break
            body, retrieved_at, url = self._grouped_day(market_date)
            if body["resultsCount"] == 0:
                continue
            return self._normalize_grouped(body, requested, market_date, cutoff,
                                           retrieved_at, url)
        return MarketDataResult(observations=[], evidence=[])

    def _grouped_day(self, market_date: date) -> tuple[dict, datetime, str]:
        retrieved_at = utc(self.clock())
        cached = self._grouped_cache.get(market_date)
        if cached is not None and timedelta(0) <= retrieved_at - cached[0] <= CACHE_TTL:
            return cached[1], cached[0], cached[2]
        url = (self.base_url + "/v2/aggs/grouped/locale/us/market/stocks/"
               + market_date.isoformat())
        try:
            response = self.client.get(
                url, headers={"Authorization": f"Bearer {self.api_key}"},
                params={"adjusted": "false"},
            )
        except httpx.RequestError as exc:
            raise ProviderError("transport", f"Massive transport failure: {type(exc).__name__}") from exc
        if response.status_code in (400, 422):
            raise ProviderError("invalid_input", f"Massive rejected request ({response.status_code})")
        if response.status_code == 429:
            raise ProviderError("rate_limit", "Massive request limit reached")
        if response.status_code >= 500:
            raise ProviderError("retryable", f"Massive unavailable ({response.status_code})")
        if response.status_code != 200:
            raise ProviderError("provider", f"Massive request failed ({response.status_code})")
        try:
            body = json.loads(response.text, parse_float=Decimal)
            if not isinstance(body, dict):
                raise ValueError("invalid grouped envelope")
            # Massive omits the optional results field on an otherwise valid
            # no-trading day (status OK, resultsCount 0).
            rows = body.get("results", [])
            if (body.get("status") != "OK"
                    or body.get("adjusted") is not False or body.get("next_url")
                    or not isinstance(body.get("request_id"), str)
                    or not body["request_id"] or not isinstance(rows, list)
                    or type(body.get("resultsCount")) is not int
                    or body["resultsCount"] != len(rows)):
                raise ValueError("invalid grouped envelope")
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ProviderError("invalid_data", "Massive returned invalid grouped EOD data") from None
        self._grouped_cache[market_date] = (retrieved_at, body, url)
        return body, retrieved_at, url

    def _normalize_grouped(self, body: dict, symbols: set[str], market_date: date,
                           cutoff: datetime, retrieved_at: datetime,
                           url: str) -> MarketDataResult:
        as_of = (datetime.combine(market_date + timedelta(days=1),
                                  datetime.min.time(), EASTERN)
                 - timedelta(microseconds=1)).astimezone(timezone.utc)
        if as_of > cutoff:
            raise ProviderError("invalid_data", "Massive grouped EOD exceeds cutoff")
        observations = []
        evidence = []
        seen = set()
        try:
            for bar in body["results"]:
                if not isinstance(bar, dict) or bar.get("T") not in symbols:
                    continue
                symbol = bar["T"]
                timestamp = bar.get("t")
                if symbol in seen or type(timestamp) is not int:
                    raise ValueError("duplicate or invalid grouped bar")
                if datetime.fromtimestamp(timestamp / 1000, timezone.utc).astimezone(EASTERN).date() != market_date:
                    raise ValueError("grouped bar date mismatch")
                value = bar.get("c")
                if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
                    raise ValueError("invalid grouped close")
                price = Decimal(str(value))
                if not price.is_finite() or price <= 0:
                    raise ValueError("invalid grouped close")
                seen.add(symbol)
                payload = {"symbol": symbol, "price": str(price), "currency": "USD",
                           "as_of": as_of.isoformat(), "provider": self.name,
                           "session_date": market_date.isoformat(),
                           "price_basis": "unadjusted"}
                source_record_id = f"{body['request_id']}:{symbol}:{timestamp}"
                digest = _hash({"observation": payload,
                                "source_record_id": source_record_id})
                evidence_id = "E" + digest[7:23]
                observations.append(PriceObservation(
                    symbol=symbol, price=price, as_of=as_of, provider=self.name,
                    evidence_id=evidence_id,
                ))
                evidence.append(EvidenceItem(
                    evidence_id=evidence_id, evidence_type="market_data",
                    provider=self.name, symbol=symbol,
                    source_uri=url + "?adjusted=false",
                    source_record_id=source_record_id, retrieved_at=retrieved_at,
                    as_of=as_of, content_hash=digest, structured_data=payload,
                ))
        except (ValueError, TypeError, KeyError, OverflowError, InvalidOperation, ValidationError):
            raise ProviderError("invalid_data", "Massive returned invalid grouped EOD data") from None
        return MarketDataResult(observations=observations, evidence=evidence)


class FailoverEODProvider:
    """Use fallback only for primary provider failures, never invalid input."""

    name = "primary_fallback"

    def __init__(self, primary: EODProvider, fallback: EODProvider):
        self.primary = primary
        self.fallback = fallback

    def retrieve_eod(self, symbols: list[str], as_of: datetime) -> MarketDataResult:
        cutoff = _validate_request(symbols, as_of)
        try:
            return self.primary.retrieve_eod(symbols, cutoff)
        except ProviderError as primary_error:
            if primary_error.semantic:
                raise
            try:
                return self.fallback.retrieve_eod(symbols, cutoff)
            except ProviderError as fallback_error:
                raise ProviderError("both_failed", f"primary {primary_error.code}; fallback {fallback_error.code}") from fallback_error
