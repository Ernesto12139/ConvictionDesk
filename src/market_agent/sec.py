"""SEC EDGAR retrieval, filing text chunking, and FTS5 evidence search."""

import hashlib
import json
import re
from decimal import Decimal
from datetime import datetime
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import quote, urljoin, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, field_validator

from .clock import Clock, system_utc_now
from .domain import EvidenceItem, FrozenModel, utc
from .persistence import Store
from .provider import ProviderError, SYMBOL


SEC_WWW = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
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
    ):
        self.store = store
        configured_user_agent = user_agent.strip()
        self.user_agent = (
            f"MarketIntelligenceAgent/0.1 contact {configured_user_agent}"
            if configured_user_agent and "@" in configured_user_agent and " " not in configured_user_agent
            else configured_user_agent
        )
        self.client = client or httpx.Client(timeout=20, follow_redirects=True)
        self.clock = clock or system_utc_now
        self.www_base_url = www_base_url.rstrip("/")
        self.data_base_url = data_base_url.rstrip("/")
        self._ciks = {}
        self._listing_identity = {}
        self._filings = {}
        self.retriever = retriever

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

    def _request(self, url: str) -> httpx.Response:
        try:
            response = self.client.get(url, headers=self._headers())
        except httpx.TimeoutException:
            raise ProviderError("timeout", "SEC request timed out") from None
        except httpx.RequestError:
            raise ProviderError("transport", "SEC transport failure") from None
        if response.status_code == 429 or response.status_code >= 500:
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

    def _resolve_cik(self, symbol: str) -> str:
        if symbol in self._ciks:
            return self._ciks[symbol]
        raw = self._json(f"{self.www_base_url}/files/company_tickers.json")
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

    def _recent_filings(self, cik: str) -> list[dict]:
        if cik in self._filings and (utc(self.clock()) - self._filings[cik][0]).total_seconds() < 300:
            return [dict(row) for row in self._filings[cik][1]]
        raw = self._json(f"{self.data_base_url}/submissions/CIK{cik}.json")
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
        lengths = {len(values) for values in columns.values()}
        if len(lengths) != 1:
            raise ProviderError("invalid_data", "SEC submissions columns are invalid")
        rows = []
        items = recent.get('items', [''] * len(columns['form']))
        if not isinstance(items,list) or len(items)!=len(columns['form']) or any(not isinstance(x,str) for x in items):
            raise ProviderError('invalid_data','SEC item metadata is invalid')
        for accession, form, accepted, primary, item in zip(
            columns["accessionNumber"], columns["form"],
            columns["acceptanceDateTime"], columns["primaryDocument"], items, strict=True,
        ):
            if not isinstance(accession, str) or not ACCESSION.fullmatch(accession):
                raise ProviderError("invalid_data", "SEC accession number is invalid")
            if not isinstance(form, str) or not isinstance(primary, str):
                raise ProviderError("invalid_data", "SEC filing metadata is invalid")
            if not isinstance(accepted, str):
                raise ProviderError("invalid_data", "SEC acceptance timestamp is invalid")
            try:
                filed_at = utc(datetime.fromisoformat(accepted.replace("Z", "+00:00")))
            except ValueError as exc:
                raise ProviderError("invalid_data", "SEC acceptance timestamp is invalid") from exc
            rows.append({
                "accession_number": accession,
                "form": form,
                "filed_at": filed_at,
                "primary_document": primary,
                "items": item,
            })
        self._filings[cik] = (utc(self.clock()), rows)
        return [dict(row) for row in rows]

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
