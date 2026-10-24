"""SEC EDGAR retrieval, filing text chunking, and FTS5 evidence search."""

import hashlib
import json
import re
import time
from decimal import Decimal
from datetime import date, datetime, time as clock_time, timedelta
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import quote, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .clock import Clock, system_utc_now
from .domain import EvidenceItem, FrozenModel, utc
from .persistence import Store
from .provider import ProviderError, SYMBOL


SEC_WWW = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
# EDGAR timestamps are Eastern. A date-only row is only knowable after that whole
# Eastern day has passed, so the declared timezone is part of the PIT contract.
SEC_TIMEZONE_NAME = "America/New_York"
SEC_TIMEZONE = ZoneInfo(SEC_TIMEZONE_NAME)
SUBMISSIONS_TTL = timedelta(seconds=300)
# Preserve the Owner's conservative <=4/sec public-SEC request policy.
SEC_MIN_REQUEST_INTERVAL = 0.3
SEC_RETRY_ATTEMPTS = 2
SEC_RETRY_DELAY = 0.3
ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
DOCUMENT_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
ITEM_HEADING = re.compile(r"^item\s+\d+[a-z]?\.?\s*", re.IGNORECASE)
BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"}
IGNORED_TAGS = {"script", "style", "noscript"}


def _hash(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SecDocument(FrozenModel):
    document_id: str
    symbol: str
    cik: str
    accession_number: str
    form: str
    filed_at: datetime
    retrieved_at: datetime
    source_uri: str
    content_hash: str

    _filed_utc = field_validator("filed_at")(utc)
    _retrieved_utc = field_validator("retrieved_at")(utc)


class SecChunk(FrozenModel):
    chunk_id: str
    document_id: str
    sequence: int
    section: str
    text: str
    content_hash: str


class SecSearchChunk(SecChunk):
    symbol: str
    cik: str
    accession_number: str
    form: str
    filed_at: datetime
    retrieved_at: datetime
    source_uri: str

    _filed_utc = field_validator("filed_at")(utc)
    _retrieved_utc = field_validator("retrieved_at")(utc)

    @classmethod
    def from_payloads(cls, chunk: dict, document: dict) -> "SecSearchChunk":
        return cls(**chunk, **{
            key: document[key]
            for key in (
                "symbol", "cik", "accession_number", "form", "filed_at",
                "retrieved_at", "source_uri",
            )
        })


class SECSearchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cik: str
    indexed_documents: int
    evidence: list[EvidenceItem]
    retrieval_mode: Literal['FTS5','HYBRID'] = 'FTS5'
    retrieval_fallback: str | None = None
    retrieval_model: str | None = None


class SecIssuerIdentity(FrozenModel):
    """Who SEC says this symbol is, and when we were told."""

    symbol: str
    cik: str
    name: str | None = None
    tickers: list[str] = Field(default_factory=list)
    mapping_source_uri: str
    mapping_observed_at: datetime


class SecFilingRow(FrozenModel):
    """One disclosure, normalized for point-in-time use. No impact is inferred."""

    accession: str
    form: str
    base_form: str
    is_amendment: bool
    items: list[str]
    accepted_at: datetime | None
    time_precision: Literal["SECOND", "DATE", "UNKNOWN"]
    filing_date: date | None
    available_at: datetime | None
    source_url: str | None
    raw_metadata_hash: str


class SecExcludedRow(FrozenModel):
    accession: str
    form: str
    reason: str


class SecRecentFilings(FrozenModel):
    schema_version: Literal["ivs-sec-provider-v1"] = "ivs-sec-provider-v1"
    issuer: SecIssuerIdentity
    rows: list[SecFilingRow]
    excluded: list[SecExcludedRow]
    as_of: datetime
    retrieved_at: datetime
    source: Literal["SEC_EDGAR_SUBMISSIONS"] = "SEC_EDGAR_SUBMISSIONS"
    source_uri: str
    from_cache: bool
    limitations: list[str] = Field(default_factory=list)


def normalize_acceptance(value: object) -> tuple[datetime | None, str]:
    """Acceptance instant plus its precision. Never invents a midnight.

    SEC returns `2026-10-22T15:46:52.000Z` for modern filings, but the column can
    be a bare date or empty. A date is not an instant: it yields DATE precision and
    no accepted_at, so no caller can mistake it for an intraday moment. A naive
    datetime is rejected rather than assumed to be UTC.
    """
    if value is None:
        return None, "UNKNOWN"
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("acceptance datetime has no timezone")
        return utc(value), "SECOND"
    if isinstance(value, date):
        return None, "DATE"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, "UNKNOWN"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return None, "DATE"
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None, "UNKNOWN"
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("acceptance datetime has no timezone")
        return utc(parsed), "SECOND"
    return None, "UNKNOWN"


def parse_filing_date(value: object) -> date | None:
    """`filingDate` is its own column and is never sliced out of an instant."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return utc(value).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def eligibility_bound(
    accepted_at: datetime | None,
    precision: str,
    filing_date: date | None,
    *,
    timezone: ZoneInfo | None = SEC_TIMEZONE,
) -> datetime | None:
    """Earliest instant this disclosure may be treated as knowable.

    A second-precision acceptance is the instant itself. A date-only row becomes
    eligible only once the entire declared day has passed, because SEC publishes
    the date and not the moment. Without a declared timezone the bound is unknown
    and the caller must fail closed.
    """
    if precision == "SECOND" and accepted_at is not None:
        return utc(accepted_at)
    if precision == "DATE" and filing_date is not None and timezone is not None:
        following = filing_date + timedelta(days=1)
        return utc(datetime.combine(following, clock_time.min, tzinfo=timezone))
    return None


def start_of_disclosed_day(filing_date: date, *, timezone: ZoneInfo | None = SEC_TIMEZONE) -> datetime | None:
    """Conservative earliest instant of a date-only disclosure, for strict cutoffs."""
    if filing_date is None or timezone is None:
        return None
    return utc(datetime.combine(filing_date, clock_time.min, tzinfo=timezone))


def filing_source_url(
    www_base: str, cik: str, accession: str, primary_document: object
) -> str | None:
    """A SEC Archives URL bound to this CIK and accession, or nothing.

    The document name is validated against SEC's own filename charset, so a
    traversal or a foreign host cannot be introduced through filing metadata.
    """
    if not isinstance(cik, str) or not re.fullmatch(r"\d{10}", cik):
        return None
    if not isinstance(accession, str) or not ACCESSION.fullmatch(accession):
        return None
    base = (www_base or "").rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in {"https", "http"} or not parts.netloc or parts.query or parts.fragment:
        return None
    directory = f"{base}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"
    if isinstance(primary_document, str) and DOCUMENT_NAME.fullmatch(primary_document):
        return f"{directory}/{primary_document}"
    return f"{directory}/"


def filing_metadata_hash(
    *, accession: str, form: str, acceptance: object, filing_date: object,
    items: object, primary_document: object,
) -> str:
    """Stable hash of the public metadata a capture and a live read must agree on."""
    return _hash({
        "accession": accession, "form": form,
        "accepted": acceptance if isinstance(acceptance, str) else None,
        "filed": str(filing_date) if filing_date else None,
        "items": items, "primary": primary_document,
    })


def _items_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(code).strip() for code in value if str(code).strip()]
    return [code.strip() for code in str(value).split(",") if code.strip()]


class _FilingTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs):
        if tag in IGNORED_TAGS:
            self.ignored_depth += 1
        elif self.ignored_depth == 0 and tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str):
        if tag in IGNORED_TAGS:
            self.ignored_depth = max(0, self.ignored_depth - 1)
        elif self.ignored_depth == 0 and tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str):
        if self.ignored_depth == 0:
            self.parts.append(data)

    def lines(self) -> list[str]:
        text = "".join(self.parts).replace("\xa0", " ")
        return [
            re.sub(r"\s+", " ", line).strip()
            for line in text.splitlines()
            if line.strip()
        ]


def parse_filing_chunks(document_id: str, html: str, *, max_chars: int = 1200) -> list[SecChunk]:
    if max_chars < 200:
        raise ValueError("SEC chunk size is too small")
    parser = _FilingTextParser()
    parser.feed(html)
    sections: list[tuple[str, list[str]]] = []
    section = "Document"
    lines: list[str] = []
    for line in parser.lines():
        if ITEM_HEADING.match(line):
            if lines:
                sections.append((section, lines))
            section, lines = line[:200], []
        else:
            lines.append(line)
    if lines:
        sections.append((section, lines))

    chunks: list[SecChunk] = []
    for section, section_lines in sections:
        current = ""
        for line in section_lines:
            words = line.split()
            while words:
                remaining = max_chars - len(current) - (1 if current else 0)
                if remaining <= 0:
                    current = _append_chunk(chunks, document_id, section, current)
                    continue
                piece_words: list[str] = []
                piece_length = 0
                while words and piece_length + len(words[0]) + (1 if piece_words else 0) <= remaining:
                    word = words.pop(0)
                    piece_words.append(word)
                    piece_length += len(word) + (1 if len(piece_words) > 1 else 0)
                if not piece_words:
                    piece_words.append(words.pop(0)[:remaining])
                piece = " ".join(piece_words)
                current = f"{current} {piece}".strip()
                if words:
                    current = _append_chunk(chunks, document_id, section, current)
        if current:
            _append_chunk(chunks, document_id, section, current)
    return chunks


def _append_chunk(chunks: list[SecChunk], document_id: str, section: str, text: str) -> str:
    sequence = len(chunks)
    digest = _hash({
        "document_id": document_id,
        "sequence": sequence,
        "section": section,
        "text": text,
    })
    chunks.append(SecChunk(
        chunk_id="SC" + digest[7:23],
        document_id=document_id,
        sequence=sequence,
        section=section,
        text=text,
        content_hash=digest,
    ))
    return ""


class SECEDGARProvider:
    """Use SEC-maintained ticker/submissions data and primary filing documents."""

    name = "sec_edgar"

    def __init__(
        self,
        store: Store,
        user_agent: str,
        *,
        client: httpx.Client | None = None,
        clock: Clock | None = None,
        www_base_url: str = SEC_WWW,
        data_base_url: str = SEC_DATA,
        retriever=None,
        min_request_interval: float = SEC_MIN_REQUEST_INTERVAL,
        retry_delay: float = SEC_RETRY_DELAY,
    ):
        self.store = store
        configured_user_agent = user_agent.strip()
        self.user_agent = (
            f"MarketIntelligenceAgent/0.1 contact {configured_user_agent}"
            if configured_user_agent and "@" in configured_user_agent and " " not in configured_user_agent
            else configured_user_agent
        )
        self.client = client or httpx.Client(timeout=20, follow_redirects=False)
        self.clock = clock or system_utc_now
        self.www_base_url = www_base_url.rstrip("/")
        self.data_base_url = data_base_url.rstrip("/")
        self._ciks = {}
        self._listing_identity = {}
        self._filings = {}
        self._submissions = {}
        self.retriever = retriever
        # Fair access is a rate contract, not a suggestion: keep a floor between
        # requests and let tests disable it rather than patch time.
        self.min_request_interval = max(0.0, float(min_request_interval))
        self.retry_delay = max(0.0, float(retry_delay))
        self._last_request_at = None
        self._public_access_stopped = None

    def search(
        self,
        symbol: str,
        query: str,
        forms: list[str],
        as_of: datetime,
        limit: int = 5,
        *, document_limit: int = 6,
    ) -> SECSearchResult:
        if not self.user_agent:
            raise ProviderError("configuration", "SEC_USER_AGENT is not configured")
        if not SYMBOL.fullmatch(symbol):
            raise ProviderError("invalid_input", "invalid US equity symbol")
        try:
            cutoff = utc(as_of)
        except (TypeError, ValueError) as exc:
            raise ProviderError("invalid_input", "SEC cutoff must be timezone-aware") from exc
        if not query.strip() or not forms or limit < 1 or limit > 20 or not 1 <= document_limit <= 6:
            raise ProviderError("invalid_input", "invalid SEC search request")

        cik = self._resolve_cik(symbol)
        rows = self._recent_filings(cik)
        candidates = [
            row for row in rows
            if row["form"] in forms and row["filed_at"] <= cutoff
        ]
        candidates.sort(key=lambda row: row["filed_at"], reverse=True)
        indexed = 0
        # Recent event forms must not crowd out the latest operating report.
        reports = [r for r in candidates if r["form"] in {"10-K", "10-Q", "20-F", "40-F"}]
        events = [r for r in candidates if r["form"] not in {"10-K", "10-Q", "20-F", "40-F"}]
        earnings = next((r for r in events if r['form']=='8-K' and '2.02' in r.get('items','')), None)
        selected_events = events[:4]
        if earnings and earnings not in selected_events:
            selected_events = [*selected_events[:3], earnings]
        chosen = sorted([*reports[:2], *selected_events], key=lambda r: r["filed_at"], reverse=True)
        if document_limit==1:
            chosen = [earnings or (reports[0] if reports else chosen[0])] if chosen else []
        else:
            chosen = chosen[:document_limit]
        for row in chosen:
            document = self._retrieve_document(symbol, cik, row)
            chunks = parse_filing_chunks(document.document_id, row["html"])
            if chunks:
                self.store.index_sec_document(document, chunks)
                indexed += 1
            # One directly linked earnings release, from the same accession only.
            # Missing links remain an explicit coverage limitation; no web crawl.
            if row is earnings:
                exhibit = self._earnings_exhibit(document.source_uri, row['html'])
                if exhibit:
                    exhibit_row = {**row, 'primary_document':exhibit}
                    release = self._retrieve_document(symbol, cik, exhibit_row)
                    release_chunks = parse_filing_chunks(release.document_id, exhibit_row['html'])
                    if release_chunks:
                        self.store.index_sec_document(release, release_chunks)
                        indexed += 1

        retrieval = self.retriever.search(symbol,query,cutoff,forms,limit) if self.retriever else None
        ranked = retrieval.chunks if retrieval else self.store.search_sec_chunks(symbol, query, cutoff, forms, limit)
        evidence = [self._evidence(chunk) for chunk in ranked]
        return SECSearchResult(cik=cik, indexed_documents=indexed, evidence=evidence,
            retrieval_mode=retrieval.effective_mode if retrieval else 'FTS5',
            retrieval_fallback=retrieval.fallback_reason if retrieval else None,
            retrieval_model=retrieval.model_fingerprint if retrieval else None)

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"}

    def _throttle(self) -> None:
        if self.min_request_interval <= 0:
            return
        if self._last_request_at is not None:
            wait = self.min_request_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _request(self, url: str) -> httpx.Response:
        from .tool_deadline import check_tool_deadline, provider_get
        if self._public_access_stopped:
            raise ProviderError(self._public_access_stopped, 'SEC access stopped for this provider session')
        self._throttle()
        try:
            response = provider_get(self.client, url, headers=self._headers(), follow_redirects=False)
            check_tool_deadline()
        except httpx.TimeoutException:
            raise ProviderError("timeout", "SEC request timed out") from None
        except httpx.RequestError:
            raise ProviderError("transport", "SEC transport failure") from None
        if response.status_code in (403, 429):
            self._public_access_stopped = 'rate_limited' if response.status_code == 429 else 'access_denied'
            raise ProviderError(self._public_access_stopped, 'SEC access stopped for this provider session')
        if response.status_code >= 500:
            raise ProviderError("retryable", f"SEC unavailable ({response.status_code})")
        if response.status_code in (400, 404, 422):
            raise ProviderError("invalid_input", f"SEC rejected request ({response.status_code})")
        if response.status_code != 200:
            raise ProviderError("provider", f"SEC request failed ({response.status_code})")
        return response

    def _json(self, url: str) -> object:
        try:
            return self._request(url).json()
        except ValueError as exc:
            raise ProviderError("invalid_data", "SEC returned malformed JSON") from exc

    def _json_with_retry(self, url: str, *, attempts: int = SEC_RETRY_ATTEMPTS) -> object:
        """Retry only transient failures. Malformed data is an answer, not a hiccup."""
        attempt = 0
        while True:
            try:
                return self._json(url)
            except ProviderError as exc:
                attempt += 1
                if exc.code not in {"retryable", "timeout"} or attempt >= max(1, attempts):
                    raise
                if self.retry_delay:
                    time.sleep(self.retry_delay * attempt)

    def _resolve_cik(self, symbol: str) -> str:
        if symbol in self._ciks:
            return self._ciks[symbol]
        raw = self._json_with_retry(f"{self.www_base_url}/files/company_tickers.json")
        if not isinstance(raw, dict):
            raise ProviderError("invalid_data", "SEC ticker data is invalid")
        for record in raw.values():
            if isinstance(record, dict) and str(record.get("ticker", "")).upper() == symbol:
                cik = record.get("cik_str")
                if type(cik) not in (int, str) or isinstance(cik, bool) or not str(cik).isdigit():
                    break
                self._ciks[symbol] = str(cik).zfill(10)
                # Retain issuer identity from the already requested SEC map.
                # This is a retrieval-time listing snapshot, not a claim that
                # different classes have equivalent economic rights/prices.
                tickers=sorted({str(r.get('ticker','')).upper() for r in raw.values()
                    if isinstance(r,dict) and str(r.get('cik_str','')).isdigit()
                    and int(r['cik_str'])==int(cik) and r.get('ticker')})
                self._listing_identity[self._ciks[symbol]]={'cik':self._ciks[symbol],
                    'tickers':tickers,'observed_at':utc(self.clock()).isoformat(),
                    'source_uri':f'{self.www_base_url}/files/company_tickers.json'}
                return self._ciks[symbol]
        raise ProviderError("not_found", f"SEC CIK not found for {symbol}")

    def _submissions_payload(self, cik: str) -> dict:
        """One bounded read of SEC submissions, cached briefly and content-hashed.

        The hash covers the public metadata columns actually consumed, so a capture
        can be replayed and compared without storing any document body.
        """
        now = utc(self.clock())
        cached = self._submissions.get(cik)
        if cached is not None and (now - cached["fetched_at"]).total_seconds() < SUBMISSIONS_TTL.total_seconds():
            return {**cached, "from_cache": True}
        url = f"{self.data_base_url}/submissions/CIK{cik}.json"
        raw = self._json_with_retry(url)
        if not isinstance(raw, dict):
            raise ProviderError("invalid_data", "SEC submissions data is invalid")
        try:
            recent = raw["filings"]["recent"]
            columns = {
                name: recent[name]
                for name in ("accessionNumber", "form", "acceptanceDateTime", "primaryDocument")
            }
        except (KeyError, TypeError) as exc:
            raise ProviderError("invalid_data", "SEC submissions data is invalid") from exc
        if any(not isinstance(values, list) for values in columns.values()):
            raise ProviderError("invalid_data", "SEC submissions columns are invalid")
        if len({len(values) for values in columns.values()}) != 1:
            raise ProviderError("invalid_data", "SEC submissions columns are invalid")
        count = len(columns["form"])
        items = recent.get("items", [""] * count)
        if (not isinstance(items, list) or len(items) != count
                or any(not isinstance(value, str) for value in items)):
            raise ProviderError("invalid_data", "SEC item metadata is invalid")
        filing_dates = recent.get("filingDate")
        if not isinstance(filing_dates, list) or len(filing_dates) != count:
            # A missing filingDate is not a reason to reject the filing; it is a
            # reason not to claim a filing date. It is never sliced from a timestamp.
            filing_dates = [None] * count
        rows = []
        seen: set[str] = set()
        for accession, form, accepted, primary, item, filed in zip(
            columns["accessionNumber"], columns["form"], columns["acceptanceDateTime"],
            columns["primaryDocument"], items, filing_dates, strict=True,
        ):
            if not isinstance(accession, str) or not ACCESSION.fullmatch(accession):
                raise ProviderError("invalid_data", "SEC accession number is invalid")
            if accession in seen:
                # One accession is one disclosure. A repeated row is the same filing,
                # never a second event and never additional corroboration.
                continue
            seen.add(accession)
            if not isinstance(form, str) or not form.strip():
                raise ProviderError("invalid_data", "SEC form is invalid")
            try:
                accepted_at, precision = normalize_acceptance(accepted)
            except ValueError as exc:
                raise ProviderError("invalid_data", "SEC acceptance timestamp is invalid") from exc
            filing_date = parse_filing_date(filed)
            rows.append({
                "accession": accession,
                "form": form.strip(),
                "base_form": form.split("/")[0].strip(),
                "is_amendment": form.strip().endswith("/A"),
                "items": _items_list(item),
                "accepted_at": accepted_at,
                "time_precision": precision,
                "filing_date": filing_date,
                "available_at": eligibility_bound(accepted_at, precision, filing_date),
                "primary_document": primary if isinstance(primary, str) else None,
                "raw_metadata_hash": filing_metadata_hash(
                    accession=accession, form=form, acceptance=accepted,
                    filing_date=filing_date, items=item, primary_document=primary),
            })
        payload = {
            "fetched_at": now,
            "name": raw.get("name") if isinstance(raw.get("name"), str) else None,
            "rows": rows,
            "url": url,
            "raw_hash": _hash({"cik": cik, "rows": [row["raw_metadata_hash"] for row in rows]}),
            "from_cache": False,
        }
        self._submissions[cik] = payload
        return payload

    def _recent_filings(self, cik: str) -> list[dict]:
        """Legacy shape kept compatible for the existing search/fundamentals callers.

        `filed_at` is the conservative eligibility instant: a date-only row uses the
        end of its declared day instead of a fabricated midnight, and a row whose
        time cannot be established at all is omitted rather than guessed.
        """
        rows = []
        for row in self._submissions_payload(cik)["rows"]:
            available = row["available_at"] or row["accepted_at"]
            if available is None or not row["primary_document"]:
                continue
            rows.append({
                "accession_number": row["accession"],
                "form": row["form"],
                "filed_at": available,
                "primary_document": row["primary_document"],
                "items": ",".join(row["items"]),
            })
        self._filings[cik] = (utc(self.clock()), rows)
        return [dict(row) for row in rows]

    def recent_filings(self, symbol: str, *, as_of: datetime) -> SecRecentFilings:
        """Public, point-in-time read of one issuer's recent filings.

        The Today projection uses only this method: no underscore access, no document
        download, no model call. A disclosure cannot appear before it was knowable,
        and rows excluded by that rule are reported instead of silently dropped.
        """
        if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
            raise ProviderError("invalid_input", "symbol is not a valid US ticker")
        cutoff = utc(as_of)
        cik = self._resolve_cik(symbol)
        payload = self._submissions_payload(cik)
        identity = self._listing_identity.get(cik) or {}
        observed = identity.get("observed_at")
        issuer = SecIssuerIdentity(
            symbol=symbol, cik=cik, name=payload["name"],
            tickers=list(identity.get("tickers") or [symbol]),
            mapping_source_uri=identity.get("source_uri")
            or f"{self.www_base_url}/files/company_tickers.json",
            mapping_observed_at=utc(datetime.fromisoformat(observed)) if observed else payload["fetched_at"],
        )
        rows, excluded = [], []
        for row in payload["rows"]:
            available = row["available_at"]
            if available is None:
                excluded.append(SecExcludedRow(accession=row["accession"], form=row["form"],
                                               reason="TIME_PRECISION_UNKNOWN"))
                continue
            if available > cutoff:
                excluded.append(SecExcludedRow(accession=row["accession"], form=row["form"],
                                               reason="AFTER_AS_OF"))
                continue
            rows.append(SecFilingRow(
                accession=row["accession"], form=row["form"], base_form=row["base_form"],
                is_amendment=row["is_amendment"], items=row["items"],
                accepted_at=row["accepted_at"], time_precision=row["time_precision"],
                filing_date=row["filing_date"], available_at=available,
                source_url=filing_source_url(self.www_base_url, cik, row["accession"],
                                             row["primary_document"]),
                raw_metadata_hash=row["raw_metadata_hash"],
            ))
        rows.sort(key=lambda row: (-row.available_at.timestamp(), row.accession))
        limitations = [
            "SEC submissions returns the issuer's most recent filings, not its full history.",
            "Per-issuer metadata only; no full-market or sector coverage is implied.",
            "No document body is downloaded and no item code is treated as an impact estimate.",
        ]
        if excluded:
            limitations.append(
                f"{len(excluded)} filing row(s) excluded by point-in-time or time-precision rules.")
        return SecRecentFilings(
            issuer=issuer, rows=rows, excluded=excluded, as_of=cutoff,
            retrieved_at=payload["fetched_at"], source_uri=payload["url"],
            from_cache=payload["from_cache"], limitations=limitations,
        )

    def fast_search(self, symbol, query, forms, as_of, limit=2):
        return self.search(symbol,query,forms,as_of,limit,document_limit=1)

    def targeted_search(self, symbol, query, forms, as_of, limit=6):
        """One focused search across the already indexed official reports; no re-download."""
        chunks = self.store.search_sec_chunks(symbol, query, as_of, forms, limit)
        return SECSearchResult(cik=self._ciks.get(symbol, ""), indexed_documents=0,
                               evidence=[self._evidence(chunk) for chunk in chunks])

    def fundamentals(self, symbol, as_of):
        """Small USD-only XBRL substrate with accession/acceptance and exact period."""
        cutoff = utc(as_of)
        cik = self._resolve_cik(symbol)
        filings = {r["accession_number"]: r for r in self._recent_filings(cik)
                   if r["filed_at"] <= cutoff}
        url = f"{self.data_base_url}/api/xbrl/companyfacts/CIK{cik}.json"
        raw = self._json(url)
        gaap = raw.get("facts", {}).get("us-gaap", {})
        concepts = {
            "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
            "net_income": ["NetIncomeLoss"],
            "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
            "cash": ["CashAndCashEquivalentsAtCarryingValue"],
        }
        metrics = []
        for label, tags in concepts.items():
            for tag in tags:
                rows = gaap.get(tag, {}).get("units", {}).get("USD", [])
                eligible = []
                for row in rows:
                    if row.get("accn") not in filings or row.get("form") not in {"10-Q", "10-K"}:
                        continue
                    try:
                        end = datetime.fromisoformat(row["end"]).date()
                        start = datetime.fromisoformat(row["start"]).date() if row.get("start") else None
                        value = Decimal(str(row["val"]))
                        duration = (end-start).days if start else None
                    except (KeyError, ValueError, TypeError):
                        continue
                    if not value.is_finite() or end > cutoff.date():
                        continue
                    # Annual/YTD cash flow is labelled by its actual start/end; no TTM inference.
                    if label != "cash" and (duration is None or duration < 60 or duration > 380):
                        continue
                    eligible.append((end, filings[row["accn"]]["filed_at"], duration or 0, row, value))
                if eligible:
                    # Revenue/net income should expose the latest reported
                    # quarter when it shares an end date with year-to-date data.
                    _, available, _, row, value = max(eligible,
                        key=lambda x:(x[0],x[1],int(60 <= x[2] <= 120) if label in {'revenue','net_income'} else x[2]))
                    metrics.append({"metric": label, "concept": tag, "value": str(value), "unit": "USD",
                        "start": row.get("start"), "end": row["end"], "form": row["form"],
                        "accession_number": row["accn"], "available_at": available.isoformat()})
        latest = {}
        for metric in sorted(metrics, key=lambda m: (m["end"], m["available_at"]), reverse=True):
            latest.setdefault(metric["metric"], metric)
        metrics = list(latest.values())
        # Reuse the already fetched CompanyFacts response; no extra request.
        from .financial_series import common_shares
        shares = common_shares(raw,list(filings.values()),cutoff)
        if not metrics and not shares:
            return []
        payload = {"symbol": symbol, "metrics": metrics,"share_facts":[shares] if shares else [],"limitations":
                   ["报告期间可能不同，不能混算利润率、增长率或估值倍数。", "本来源是报告事实；兼容季度的 TTM 由独立 Python 投影计算，不构造预测或目标价。"]}
        if cik in self._listing_identity:
            payload['issuer_listing_identity']=self._listing_identity[cik]
        digest = _hash(payload)
        summary = EvidenceItem(evidence_id="E"+digest[7:23], evidence_type="sec_filing", provider=self.name,
            symbol=symbol, title="SEC XBRL · 报告财务事实", source_uri=url,
            source_record_id=f"companyfacts:{cik}", published_at=max(datetime.fromisoformat(m["available_at"]) for m in [*metrics,*([shares] if shares else [])]),
            retrieved_at=utc(self.clock()), content_hash=digest,
            excerpt="；".join(f"{m['metric']} {m['value']} {m['unit']} ({m['start'] or ''} / {m['end']})" for m in metrics),
            structured_data=payload)
        from .financial_series import quarterly_series
        result = [summary]
        for quarter in quarterly_series(raw,list(filings.values()),cutoff):
            computed = {'calculation':'sec_quarterly_v1','symbol':symbol,'quarter':quarter,
                'source_evidence_ids':[summary.evidence_id],'source_uri':url,
                'limitations':['Exact reported periods; YTD differences and FCF are Python-derived.',
                    'EPS is never derived by subtraction; missing periods remain missing.']}
            digest = _hash(computed)
            available = max(datetime.fromisoformat(row['available_at']) for row in quarter['metrics'])
            result.append(EvidenceItem(evidence_id='E'+digest[7:23],evidence_type='calculation',
                provider='python_decimal',symbol=symbol,title='Python · SEC exact-quarter financials',
                source_uri=url,published_at=available,retrieved_at=utc(self.clock()),
                as_of=datetime.fromisoformat(quarter['end']+'T00:00:00+00:00'),content_hash=digest,
                excerpt=f"Historical quarter {quarter['start']} / {quarter['end']}: "+'; '.join(
                    f"{row['metric']} {row['value']} {row['unit']} ({row['basis']})" for row in quarter['metrics']),
                structured_data=computed))
        return result

    @staticmethod
    def _earnings_exhibit(primary_uri: str, html: str) -> str | None:
        class Links(HTMLParser):
            def __init__(self):
                super().__init__()
                self.hrefs = []
            def handle_starttag(self, tag, attrs):
                if tag.lower()=='a':
                    self.hrefs.extend(value for key,value in attrs if key.lower()=='href' and value)
        parser = Links()
        parser.feed(html)
        base = urlsplit(primary_uri)
        directory = base.path.rsplit('/',1)[0]+'/'
        for href in parser.hrefs:
            target = urlsplit(urljoin(primary_uri,href))
            name = target.path.removeprefix(directory)
            if (target.scheme==base.scheme and target.netloc==base.netloc and target.path.startswith(directory)
                    and not target.query and not target.fragment and DOCUMENT_NAME.fullmatch(name)
                    and re.search(r'(?:ex(?:hibit)?[-_]?99[-_]?1|ex991|ex99\.1)',name,re.I)
                    and name.lower().endswith(('.htm','.html'))):
                return name
        return None

    def _retrieve_document(self, symbol: str, cik: str, row: dict) -> SecDocument:
        document_parts = row["primary_document"].split("/")
        if (
            not document_parts
            or any(not part or not DOCUMENT_NAME.fullmatch(part) for part in document_parts)
            or any(part in {".", ".."} for part in document_parts)
        ):
            raise ProviderError("invalid_data", "SEC primary document path is invalid")
        accession_compact = row["accession_number"].replace("-", "")
        cik_compact = str(int(cik))
        source_uri = (
            f"{self.www_base_url}/Archives/edgar/data/{cik_compact}/"
            f"{accession_compact}/{quote(row['primary_document'], safe='/')}"
        )
        response = self._request(source_uri)
        html = response.text
        digest = _hash({
            "accession_number": row["accession_number"],
            "primary_document": row["primary_document"],
            "html": html,
        })
        row["html"] = html
        return SecDocument(
            document_id="SD" + digest[7:23],
            symbol=symbol,
            cik=cik,
            accession_number=row["accession_number"],
            form=row["form"],
            filed_at=row["filed_at"],
            retrieved_at=utc(self.clock()),
            source_uri=source_uri,
            content_hash=digest,
        )

    def _evidence(self, chunk: SecSearchChunk) -> EvidenceItem:
        return EvidenceItem(
            evidence_id="E" + chunk.content_hash[7:23],
            evidence_type="sec_filing",
            provider=self.name,
            symbol=chunk.symbol,
            title=f"{chunk.form} — {chunk.section}",
            source_uri=chunk.source_uri,
            source_record_id=chunk.chunk_id,
            published_at=chunk.filed_at,
            retrieved_at=chunk.retrieved_at,
            content_hash=chunk.content_hash,
            excerpt=chunk.text,
            structured_data={
                "cik": chunk.cik,
                "accession_number": chunk.accession_number,
                "form": chunk.form,
                "section": chunk.section,
                "chunk_id": chunk.chunk_id,
                "sequence": chunk.sequence,
            },
        )
