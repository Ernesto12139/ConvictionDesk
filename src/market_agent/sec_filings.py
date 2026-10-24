"""ivs-sec-v1: read-only SEC filing leads for Today.

A filing is the one market event this product can date to the second, cite to a public
document and forward to a model without a licensing question. `attention.ingest` only
accepts news receipts, so filings never become Attention cards; this module is the read
side that fills that gap without touching the retained packet.

What this module refuses to do:

- no model call, no score from an LLM, no packet write, no schema change, no scan;
- no impact, earnings or price claim: an item code says what SEC asked about, never
  what it is worth;
- no fabricated instants: a date-only disclosure is DATE precision and is only eligible
  after its declared day has passed;
- no silent emptiness: not-scanned, refused, unknown and genuinely-empty are four
  different answers, and a refused source never reads as "no filings".

The source is injected and only its public `recent_filings` method is used, so the same
code runs against EDGAR and against a frozen capture.
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta
from typing import Iterable, Literal, Protocol

from pydantic import Field

from .domain import FrozenModel, utc
from .provider import ProviderError
from .sec import (
    SEC_TIMEZONE,
    SEC_TIMEZONE_NAME,
    SecRecentFilings,
)

SCHEMA_VERSION = "ivs-sec-v1"
NORMALIZATION_VERSION = "ivs-sec-normalize-v1"
DEFAULT_LOOKBACK_DAYS = 7
MAX_SYMBOLS = 40
# A second filing on the same item codes inside this window continues the same story.
FOLLOW_DAYS = 30

SCOPE_KINDS = ("HOLDING", "WATCHING")
SCOPE_RANK = {"HOLDING": 0, "WATCHING": 1}

COVERAGE_STATUSES = (
    "NOT_SCANNED",
    "COMPLETE_WITH_FILINGS",
    "COMPLETE_NO_NEW_FILINGS",
    "PARTIAL",
    "SOURCE_UNAVAILABLE",
    "INVALID_DATA",
)

# SEC's closed set of 8-K item codes, with the question each one forces a holder to
# re-check. This is the whole "materiality dictionary": adding a phrase here would be
# the keyword list this product has repeatedly refused to build.
ITEM_EVENTS: dict[str, tuple[str, str, str]] = {
    "1.01": ("AGREEMENT", "签订重大最终协议", "这份协议改变了收入或成本的哪个既有假设？"),
    "1.02": ("AGREEMENT_TERMINATED", "终止重大最终协议", "失去这份协议会改变收入或成本结构吗？"),
    "1.03": ("BANKRUPTCY", "破产或接管", "现有股权与债权的清偿顺序是否已经改变？"),
    "2.01": ("ACQUISITION_DISPOSAL", "完成重大收购或处置", "并入或剥离的业务是否改变规模与利润率？"),
    "2.02": ("EARNINGS", "经营成果与财务状况", "收入、利润率与全年指引是否仍支持当前持有逻辑？"),
    "2.03": ("NEW_OBLIGATION", "产生直接重大财务义务", "新增义务是否压缩自由现金流与利息覆盖？"),
    "2.04": ("ACCELERATED_OBLIGATION", "触发重大加速义务", "是否触发提前偿付或契约条款？"),
    "2.05": ("EXIT_COSTS", "退出或处置活动成本", "一次性成本是否改变了当期盈利能力？"),
    "2.06": ("IMPAIRMENT", "重大减值", "减值是否说明此前对资产价值的假设已经失效？"),
    "3.01": ("DELISTING", "摘牌或转让上市", "流动性或持有条件是否改变？"),
    "4.01": ("AUDITOR_CHANGE", "更换会计师事务所", "审计意见或会计口径是否可能变化？"),
    "4.02": ("NON_RELIANCE", "前期财务报表不可依赖", "哪些历史数字与比较基数需要重算？"),
    "5.01": ("CONTROL_CHANGE", "控制权变更", "控制权变化是否改变资本配置与治理判断？"),
    "5.02": ("LEADERSHIP", "董事或高管变动", "关键执行人的变动是否影响战略落地？"),
    "7.01": ("REG_FD", "公平披露", "这次披露是否包含未反映在模型里的新信息？"),
    "8.01": ("OTHER_EVENTS", "其他事件", "这条披露是否触及当前投资逻辑的任何假设？"),
}

# Items that carry no event of their own. 9.01 only says "here are the exhibits".
EXHIBIT_ONLY = frozenset({"9.01"})
MATERIAL_ITEMS = frozenset(ITEM_EVENTS) - {"7.01", "8.01"}
ROUTINE_ITEMS = frozenset({"7.01", "8.01"})

FORM_FAMILY = {
    "10-Q": ("PERIODIC_REPORT", "季度报告"),
    "10-K": ("PERIODIC_REPORT", "年度报告"),
    "20-F": ("PERIODIC_REPORT", "外国发行人年度报告"),
    "40-F": ("PERIODIC_REPORT", "外国发行人年度报告"),
    "6-K": ("FOREIGN_INTERIM", "外国发行人中期披露"),
    "DEF 14A": ("PROXY", "股东大会委托书"),
    "SC 13D": ("OWNERSHIP", "主要股东权益披露"),
    "SC 13G": ("OWNERSHIP", "主要股东权益披露"),
    "4": ("INSIDER", "内部人交易"),
    "S-1": ("REGISTRATION", "注册声明"),
    "S-3": ("REGISTRATION", "注册声明"),
    "424B5": ("REGISTRATION", "招股说明书补充"),
}

BAND_RANK = {
    "MATERIAL_ITEM": 0,
    "SCHEDULED_REPORT": 1,
    "REGISTRATION_FILING": 2,
    "OWNERSHIP_FILING": 3,
    "ROUTINE_ITEM": 4,
    "UNCLASSIFIED": 5,
}

RANKING_MEANING = (
    "确定性排序：范围（持仓先于关注）→ SEC 条目档位 → 首次披露先于后续/修订 → "
    "可用时间。它不是模型判定的重大投资影响，也不是概率或仓位建议。"
)


# --- models -----------------------------------------------------------------

class ScopeRef(FrozenModel):
    kind: Literal["HOLDING", "WATCHING"]
    symbol: str


class FilingRights(FrozenModel):
    display: Literal["PUBLIC_FILING"] = "PUBLIC_FILING"
    model_forwarding: Literal["GRANTED", "NOT_GRANTED", "UNKNOWN"] = "GRANTED"
    basis: str
    reviewed_scope: str


class FilingProvenance(FrozenModel):
    source: Literal["SEC_EDGAR_SUBMISSIONS"] = "SEC_EDGAR_SUBMISSIONS"
    source_uri: str
    retrieved_at: datetime
    raw_metadata_hash: str
    normalization_version: str = NORMALIZATION_VERSION
    capture_identity: str


class SymbolCoverage(FrozenModel):
    symbol: str
    scope_kinds: list[str]
    status: Literal[
        "NOT_SCANNED", "COMPLETE_WITH_FILINGS", "COMPLETE_NO_NEW_FILINGS",
        "PARTIAL", "SOURCE_UNAVAILABLE", "INVALID_DATA"]
    mapping_status: Literal["RESOLVED", "NOT_FOUND", "LOOKUP_FAILED"]
    cik: str | None
    issuer_name: str | None
    as_of: datetime
    checked_at: datetime
    window_start: datetime
    window_end: datetime
    source: str
    filing_count: int
    # Two different reasons a row is not shown, kept apart on purpose: a filing older
    # than the window is normal, while one excluded by a point-in-time rule is a limit
    # on what this read could know.
    outside_window: int
    excluded_by_pit: int
    limitations: list[str] = Field(default_factory=list)


class FilingRelation(FrozenModel):
    accession: str
    form: str
    basis: str
    available_at: datetime | None


class FilingRank(FrozenModel):
    scope_rank: int
    band: str
    band_rank: int
    fresh_disclosure: bool
    basis: list[str]
    meaning: str = RANKING_MEANING


class ScreeningState(FrozenModel):
    status: Literal["UNSCREENED"] = "UNSCREENED"
    model_used: bool = False
    meaning: str = "SEC 原始申报线索：未经模型重大性分析，不代表已确认投资影响。"


class FilingEvidence(FrozenModel):
    schema_version: Literal["ivs-sec-v1"] = SCHEMA_VERSION
    evidence_id: str
    cik: str
    symbol: str
    accession: str
    form: str
    base_form: str
    is_amendment: bool
    items: list[str]
    unknown_items: list[str]
    accepted_at: datetime | None
    time_precision: Literal["SECOND", "DATE", "UNKNOWN"]
    filing_date: date | None
    available_at: datetime
    source_url: str
    scope: ScopeRef
    scope_matches: list[ScopeRef]
    coverage: SymbolCoverage
    rights: FilingRights
    provenance: FilingProvenance
    event_class: str
    event_label: str
    review_questions: list[str]
    screening: ScreeningState
    ranking: FilingRank
    amends: FilingRelation | None = None
    follows: FilingRelation | None = None


class SourceIdentity(FrozenModel):
    name: Literal["SEC_EDGAR_SUBMISSIONS"] = "SEC_EDGAR_SUBMISSIONS"
    authority: str = "U.S. Securities and Exchange Commission"
    data_uri: str
    request_policy: str
    cache_ttl_seconds: int
    timezone: str = SEC_TIMEZONE_NAME


class ProjectionCounts(FrozenModel):
    symbols_requested: int
    symbols_with_filings: int
    filings: int
    holding_filings: int
    watching_filings: int
    amendments: int
    follow_ons: int
    outside_window: int
    excluded_by_pit: int
    unknown_items: int


class FilingsProjection(FrozenModel):
    schema_version: Literal["ivs-sec-v1"] = SCHEMA_VERSION
    as_of: datetime
    status: Literal[
        "NOT_SCANNED", "COMPLETE_WITH_FILINGS", "COMPLETE_NO_NEW_FILINGS",
        "PARTIAL", "SOURCE_UNAVAILABLE", "INVALID_DATA"]
    filings: list[FilingEvidence]
    ranked_evidence_ids: list[str]
    per_symbol_coverage: list[SymbolCoverage]
    source: SourceIdentity
    counts: ProjectionCounts
    screening: ScreeningState
    ranking_meaning: str = RANKING_MEANING
    limitations: list[str] = Field(default_factory=list)


class RecentFilingsSource(Protocol):
    """The only surface this module needs. No underscore method, no document body."""

    def recent_filings(self, symbol: str, *, as_of: datetime) -> SecRecentFilings: ...


# --- pure helpers ------------------------------------------------------------

def normalize_items(raw: Iterable[str]) -> tuple[list[str], list[str]]:
    """Known SEC item codes and retained-but-unknown ones, both in filing order."""
    known: list[str] = []
    unknown: list[str] = []
    for code in raw:
        text = str(code).strip()
        if not text:
            continue
        if text in ITEM_EVENTS or text in EXHIBIT_ONLY:
            if text not in known:
                known.append(text)
        elif text not in unknown:
            # Retained, never inferred: an unknown code is not a materiality signal.
            unknown.append(text)
    return known, unknown


def classify(form: str, base_form: str, items: list[str]) -> tuple[str, str, str]:
    """(event_class, label, band) from the form and SEC's own item set."""
    if base_form == "8-K":
        for code in items:
            if code in ITEM_EVENTS:
                event_class, label, _question = ITEM_EVENTS[code]
                band = "MATERIAL_ITEM" if code in MATERIAL_ITEMS else "ROUTINE_ITEM"
                return event_class, label, band
        return "UNCLASSIFIED_EXHIBITS", "仅附件的当前报告", "UNCLASSIFIED"
    event_class, label = FORM_FAMILY.get(base_form, ("OTHER_FORM", "其他申报"))
    band = {
        "PERIODIC_REPORT": "SCHEDULED_REPORT",
        "FOREIGN_INTERIM": "SCHEDULED_REPORT",
        "PROXY": "SCHEDULED_REPORT",
        "REGISTRATION": "REGISTRATION_FILING",
        "OWNERSHIP": "OWNERSHIP_FILING",
        "INSIDER": "OWNERSHIP_FILING",
    }.get(event_class, "UNCLASSIFIED")
    return event_class, label, band


def review_questions(items: list[str], *, limit: int = 3) -> list[str]:
    """Questions the filing forces a holder to answer. Never an impact estimate."""
    output: list[str] = []
    for code in items:
        entry = ITEM_EVENTS.get(code)
        if entry is None:
            continue
        question = entry[2]
        if question not in output:
            output.append(question)
        if len(output) >= limit:
            break
    return output


def evidence_id_for(cik: str, accession: str) -> str:
    """CIK plus accession is the disclosure. Nothing else identifies it."""
    return f"sec:{cik}:{accession}"


def filing_rights() -> FilingRights:
    return FilingRights(
        basis=("SEC EDGAR 公开索引中的申报元数据与原文链接；本授权仅覆盖这些元数据，"
               "不推定申报正文或附件的版权与转发许可。"),
        reviewed_scope=("仅覆盖申报元数据（表格、条目、accession、接收时间）与主文件链接；"
                        "附件正文、第三方新闻正文与本地组合上下文不在本授权范围内。"),
    )


def resolve_scopes(*, scopes=None, entries=None) -> dict[str, list[str]]:
    """symbol -> scope kinds. Only HOLDING and WATCHING are ever scanned."""
    pairs: dict[str, set[str]] = {}

    def add(symbol, kind):
        if not isinstance(symbol, str) or not symbol:
            return
        if kind in SCOPE_KINDS:
            pairs.setdefault(symbol, set()).add(kind)

    if entries is not None:
        for entry in entries:
            add(getattr(entry, "symbol", None), getattr(entry, "state", None))
    for item in scopes or ():
        if hasattr(item, "kind") and hasattr(item, "symbol"):
            add(item.symbol, item.kind)
        else:
            symbol, kind = item
            add(symbol, kind)
    return {symbol: sorted(kinds, key=lambda value: SCOPE_RANK[value])
            for symbol, kinds in sorted(pairs.items())}


def _aggregate(statuses: list[str]) -> str:
    if not statuses:
        return "NOT_SCANNED"
    distinct = set(statuses)
    if distinct == {"COMPLETE_WITH_FILINGS"}:
        return "COMPLETE_WITH_FILINGS"
    if distinct <= {"COMPLETE_WITH_FILINGS", "COMPLETE_NO_NEW_FILINGS"}:
        return ("COMPLETE_WITH_FILINGS" if "COMPLETE_WITH_FILINGS" in distinct
                else "COMPLETE_NO_NEW_FILINGS")
    if len(distinct) == 1:
        only = next(iter(distinct))
        if only in {"SOURCE_UNAVAILABLE", "INVALID_DATA", "NOT_SCANNED"}:
            return only
    # Mixed outcomes are never reported as a complete answer.
    return "PARTIAL"


def _error_coverage(symbol, kinds, error, *, as_of, window_start, source) -> SymbolCoverage:
    code = getattr(error, "code", "")
    if code == "not_found":
        status, mapping = "NOT_SCANNED", "NOT_FOUND"
        limitation = ("SEC 的 ticker 映射中没有该标的：覆盖状态未知，不是“没有新申报”。")
    elif code == "invalid_data":
        status, mapping = "INVALID_DATA", "LOOKUP_FAILED"
        limitation = "SEC 返回的元数据无法解析；本次没有得到可用的申报列表。"
    else:
        status, mapping = "SOURCE_UNAVAILABLE", "LOOKUP_FAILED"
        limitation = f"SEC 数据源本次不可用（{code or 'unknown'}）；没有拿到申报列表。"
    return SymbolCoverage(
        symbol=symbol, scope_kinds=list(kinds), status=status, mapping_status=mapping,
        cik=None, issuer_name=None, as_of=as_of, checked_at=as_of,
        window_start=window_start, window_end=as_of, source=source,
        filing_count=0, outside_window=0, excluded_by_pit=0, limitations=[limitation],
    )


def _relation(row, prior, *, basis: str) -> FilingRelation:
    return FilingRelation(accession=prior.accession, form=prior.form, basis=basis,
                          available_at=prior.available_at)


def _prior_with_overlap(history, row, *, within_days: int | None, include_amendments: bool):
    for prior in history:
        if prior.accession == row.accession:
            continue
        if prior.available_at is None or row.available_at is None:
            continue
        if prior.available_at >= row.available_at:
            continue
        if not include_amendments and prior.is_amendment:
            continue
        if prior.base_form != row.base_form:
            continue
        if within_days is not None and (row.available_at - prior.available_at) > timedelta(days=within_days):
            continue
        # 9.01 only says "exhibits attached". Two unrelated 8-Ks both carrying it are
        # not the same story, so exhibit-only codes never create a relation.
        if (set(prior.items) & set(row.items)) - EXHIBIT_ONLY:
            return prior
    return None


def _rank_basis(scope: ScopeRef, band: str, fresh: bool, items: list[str]) -> list[str]:
    basis = [f"SCOPE_{scope.kind}"]
    material = [code for code in items if code in MATERIAL_ITEMS]
    if material:
        basis.append("SEC_ITEM_" + "+".join(material[:2]))
    else:
        basis.append(f"BAND_{band}")
    basis.append("FIRST_DISCLOSURE" if fresh else "FOLLOW_ON_OR_AMENDMENT")
    return basis


# --- projection --------------------------------------------------------------

def project(
    source: RecentFilingsSource,
    *,
    as_of: datetime,
    scopes=None,
    entries=None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_symbols: int = MAX_SYMBOLS,
    capture_identity: str | None = None,
) -> FilingsProjection:
    """Build the read-only filing projection for the current holdings and watchlist.

    Nothing here is a claim about impact. The output separates four states that are
    routinely collapsed into an empty list: not scanned, refused, unknown, and
    genuinely no filings in the window.
    """
    now = utc(as_of)
    if lookback_days < 0:
        raise ValueError("lookback_days must not be negative")
    window_start = now - timedelta(days=lookback_days)
    wanted = resolve_scopes(scopes=scopes, entries=entries)
    symbols = list(wanted)[:max(1, max_symbols)]
    truncated = len(wanted) > len(symbols)
    capture = capture_identity or f"live:{now.isoformat()}"

    coverage: dict[str, SymbolCoverage] = {}
    grouped: dict[tuple[str, str], dict] = {}
    source_name = "SEC_EDGAR_SUBMISSIONS"
    data_uri = "https://data.sec.gov/submissions/CIK{cik}.json"
    cache_ttl = 300

    for symbol in symbols:
        kinds = wanted[symbol]
        refs = [ScopeRef(kind=kind, symbol=symbol) for kind in kinds]
        primary = refs[0]
        try:
            report = source.recent_filings(symbol, as_of=now)
        except ProviderError as error:
            coverage[symbol] = _error_coverage(symbol, kinds, error, as_of=now,
                                               window_start=window_start, source=source_name)
            continue
        except Exception as error:  # an unexpected provider bug is still a coverage answer
            coverage[symbol] = _error_coverage(
                symbol, kinds, ProviderError("transport", type(error).__name__),
                as_of=now, window_start=window_start, source=source_name)
            continue

        data_uri = report.source_uri.replace(report.issuer.cik, "{cik}")
        in_window = [row for row in report.rows if row.available_at >= window_start]
        outside_window = len(report.rows) - len(in_window)
        coverage[symbol] = SymbolCoverage(
            symbol=symbol, scope_kinds=kinds,
            status="COMPLETE_WITH_FILINGS" if in_window else "COMPLETE_NO_NEW_FILINGS",
            mapping_status="RESOLVED", cik=report.issuer.cik,
            issuer_name=report.issuer.name, as_of=now, checked_at=report.retrieved_at,
            window_start=window_start, window_end=now, source=report.source,
            filing_count=len(in_window), outside_window=outside_window,
            excluded_by_pit=len(report.excluded),
            limitations=list(report.limitations),
        )
        for row in in_window:
            key = (report.issuer.cik, row.accession)
            bucket = grouped.setdefault(key, {
                "issuer": report.issuer, "row": row, "scope": primary, "matches": [],
                "history": in_window, "report": report,
            })
            # Every scope this symbol holds is an annotation on one disclosure.
            bucket["matches"].extend(refs)

    filings: list[FilingEvidence] = []
    for (cik, accession), bucket in grouped.items():
        issuer, row = bucket["issuer"], bucket["row"]
        known, unknown = normalize_items(row.items)
        event_class, label, band = classify(row.form, row.base_form, known)
        history = [item for item in bucket["history"] if item.accession != accession]
        amends = None
        follows = None
        if row.is_amendment:
            prior = _prior_with_overlap(history, row, within_days=None, include_amendments=False)
            if prior is not None:
                amends = _relation(row, prior, basis="SAME_BASE_FORM_AND_SHARED_ITEM")
        else:
            prior = _prior_with_overlap(history, row, within_days=FOLLOW_DAYS,
                                        include_amendments=False)
            if prior is not None:
                follows = _relation(row, prior, basis="SAME_ITEM_WITHIN_FOLLOW_WINDOW")
        fresh = amends is None and follows is None
        matches: list[ScopeRef] = []
        for ref in bucket["matches"]:
            if ref not in matches:
                matches.append(ref)
        primary = matches[0]
        filings.append(FilingEvidence(
            evidence_id=evidence_id_for(cik, accession),
            cik=cik, symbol=primary.symbol, accession=accession, form=row.form,
            base_form=row.base_form, is_amendment=row.is_amendment,
            items=known, unknown_items=unknown, accepted_at=row.accepted_at,
            time_precision=row.time_precision, filing_date=row.filing_date,
            available_at=row.available_at,
            source_url=row.source_url or f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/",
            scope=primary, scope_matches=matches, coverage=coverage[primary.symbol],
            rights=filing_rights(),
            provenance=FilingProvenance(
                source_uri=bucket["report"].source_uri,
                retrieved_at=bucket["report"].retrieved_at,
                raw_metadata_hash=row.raw_metadata_hash,
                capture_identity=capture,
            ),
            event_class=event_class, event_label=label,
            review_questions=review_questions(known),
            screening=ScreeningState(),
            ranking=FilingRank(
                scope_rank=SCOPE_RANK[primary.kind], band=band, band_rank=BAND_RANK[band],
                fresh_disclosure=fresh, basis=_rank_basis(primary, band, fresh, known),
            ),
            amends=amends, follows=follows,
        ))

    # Contract order: available_at descending, then accession. Stable and explainable.
    filings.sort(key=lambda item: (-item.available_at.timestamp(), item.accession))
    ranked = sorted(filings, key=lambda item: (
        item.ranking.scope_rank, item.ranking.band_rank,
        0 if item.ranking.fresh_disclosure else 1,
        -item.available_at.timestamp(), item.accession))

    ordered_coverage = [coverage[symbol] for symbol in symbols]
    status = _aggregate([item.status for item in ordered_coverage])
    counts = ProjectionCounts(
        symbols_requested=len(symbols),
        symbols_with_filings=sum(1 for item in ordered_coverage if item.filing_count),
        filings=len(filings),
        holding_filings=sum(1 for item in filings if item.scope.kind == "HOLDING"),
        watching_filings=sum(1 for item in filings if item.scope.kind == "WATCHING"),
        amendments=sum(1 for item in filings if item.is_amendment),
        follow_ons=sum(1 for item in filings if item.follows is not None),
        outside_window=sum(item.outside_window for item in ordered_coverage),
        excluded_by_pit=sum(item.excluded_by_pit for item in ordered_coverage),
        unknown_items=sum(len(item.unknown_items) for item in filings),
    )
    limitations = [
        "只覆盖持仓与关注标的，按标的读取 SEC 最近申报；不代表全市场或行业覆盖。",
        "SEC 只提供最近若干份申报，更早历史不在本次读取范围内。",
        "申报存在即事实；条目只提出应当核查什么，不构成盈利影响或股价因果判断。",
        "同一 accession 只算一次；修订申报是独立披露，不是新的印证来源。",
    ]
    if truncated:
        limitations.append(f"标的数量超过上限 {len(symbols)}，其余标的本次未扫描。")
    if status == "PARTIAL":
        limitations.append("部分标的本次没有取得可用申报列表；结果不是完整的“无新申报”。")

    return FilingsProjection(
        as_of=now, status=status, filings=filings,
        ranked_evidence_ids=[item.evidence_id for item in ranked],
        per_symbol_coverage=ordered_coverage,
        source=SourceIdentity(data_uri=data_uri, cache_ttl_seconds=cache_ttl,
                              request_policy="每次刷新每标的一次公开元数据读取；页面轮询不发起网络请求。"),
        counts=counts, screening=ScreeningState(), limitations=limitations,
    )


def by_scope(projection: FilingsProjection) -> list[dict]:
    """Holdings first, then watchlist, each in the explainable ranked order.

    Presentation only: the contract's `filings` order stays as frozen, so a consumer
    that needs the raw ordering is not affected by a UI grouping choice.
    """
    index = {item.evidence_id: item for item in projection.filings}
    ordered = [index[key] for key in projection.ranked_evidence_ids]
    return [
        {"scope": kind,
         "evidence_ids": [item.evidence_id for item in ordered if item.scope.kind == kind]}
        for kind in SCOPE_KINDS
    ]


# --- offline replay ----------------------------------------------------------

CAPTURE_SCHEMA_VERSION = "ivs-sec-capture-v1"


class CapturedFilingsSource:
    """Replay a frozen public capture through the same normalization as live reads.

    A capture stores SEC's public metadata columns verbatim (no document bodies, no
    third-party news), so a reviewer can reproduce exactly what the live provider
    would have returned at the same `as_of`. Only public data is stored, and the
    stored acceptance string is re-normalized on every replay rather than trusted.
    """

    name = "sec_edgar_capture"

    def __init__(self, capture: dict, *, www_base_url: str = "https://www.sec.gov"):
        from .sec import parse_filing_date, filing_source_url, normalize_acceptance, eligibility_bound

        if capture.get("schema_version") != CAPTURE_SCHEMA_VERSION:
            raise ValueError("unsupported capture schema")
        self._capture = capture
        self._www = www_base_url
        self._normalize = normalize_acceptance
        self._parse_date = parse_filing_date
        self._bound = eligibility_bound
        self._url = filing_source_url

    @property
    def captured_at(self) -> str | None:
        return self._capture.get("captured_at")

    def recent_filings(self, symbol: str, *, as_of: datetime) -> SecRecentFilings:
        from .sec import SecExcludedRow, SecFilingRow, SecIssuerIdentity

        issuer = self._capture["issuer"]
        if symbol != issuer["symbol"]:
            raise ProviderError("not_found", f"capture does not contain {symbol}")
        cutoff = utc(as_of)
        rows, excluded = [], []
        for row in self._capture["rows"]:
            accepted_at, precision = self._normalize(row.get("acceptance"))
            filing_date = self._parse_date(row.get("filing_date"))
            available = self._bound(accepted_at, precision, filing_date)
            if available is None:
                excluded.append(SecExcludedRow(accession=row["accession"], form=row["form"],
                                               reason="TIME_PRECISION_UNKNOWN"))
                continue
            if available > cutoff:
                excluded.append(SecExcludedRow(accession=row["accession"], form=row["form"],
                                               reason="AFTER_AS_OF"))
                continue
            rows.append(SecFilingRow(
                accession=row["accession"], form=row["form"],
                base_form=row.get("base_form") or row["form"].split("/")[0],
                is_amendment=row["form"].endswith("/A"), items=list(row.get("items") or []),
                accepted_at=accepted_at, time_precision=precision, filing_date=filing_date,
                available_at=available,
                source_url=self._url(self._www, issuer["cik"], row["accession"],
                                     row.get("primary_document")),
                raw_metadata_hash=row.get("raw_metadata_hash", ""),
            ))
        rows.sort(key=lambda row: (-row.available_at.timestamp(), row.accession))
        mapping = self._capture.get("mapping") or {}
        retrieved = datetime.fromisoformat(self._capture["captured_at"])
        return SecRecentFilings(
            issuer=SecIssuerIdentity(
                symbol=symbol, cik=issuer["cik"], name=issuer.get("name"),
                tickers=list(issuer.get("tickers") or [symbol]),
                mapping_source_uri=mapping.get("source_uri", "https://www.sec.gov/files/company_tickers.json"),
                mapping_observed_at=datetime.fromisoformat(mapping["observed_at"])
                if mapping.get("observed_at") else retrieved,
            ),
            rows=rows, excluded=excluded, as_of=cutoff, retrieved_at=retrieved,
            source_uri=self._capture["source_uri"], from_cache=True,
            limitations=["Frozen public SEC metadata capture; not a live read.",
                         "Only filing metadata is stored: no document body, no news text."],
        )


# --- route-facing cache ------------------------------------------------------

class FilingsCache:
    """Last projection held in memory, so a page poll never reads SEC.

    The contract requires that GET never dispatches a source read and that only an
    explicit POST refresh does. This holds that boundary in one place instead of
    leaving it to each route: `current()` is pure, `refresh()` is the only method
    that touches the source, and a failed refresh keeps the previous snapshot with
    its own timestamp rather than replacing it with an empty page.
    """

    def __init__(self, *, lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                 max_symbols: int = MAX_SYMBOLS, clock=None):
        from .clock import system_utc_now

        self.lookback_days = lookback_days
        self.max_symbols = max_symbols
        self._clock = clock or system_utc_now
        self._lock = threading.Lock()
        self._projection: FilingsProjection | None = None
        self._state = {"attempts": 0, "last_attempt_at": None, "last_success_at": None,
                       "last_result": None, "last_duration_ms": None,
                       "last_status": None}

    def current(self) -> FilingsProjection | None:
        with self._lock:
            return self._projection

    @property
    def state(self) -> dict:
        with self._lock:
            return dict(self._state) | {
                "as_of": self._projection.as_of.isoformat() if self._projection else None,
                "meaning": ("只有 POST 刷新会读取 SEC；GET 只返回上一次结果及其时间。"
                            "FAILED 表示本次没有取得任何可用标的，上一次结果保持不变。"),
            }

    def refresh(self, source: RecentFilingsSource, *, entries=None, scopes=None) -> FilingsProjection:
        started = time.perf_counter()
        now = utc(self._clock())
        projection = project(source, as_of=now, scopes=scopes, entries=entries,
                             lookback_days=self.lookback_days, max_symbols=self.max_symbols,
                             capture_identity=f"refresh:{now.isoformat()}")
        duration = int((time.perf_counter() - started) * 1000)
        definite = sum(1 for item in projection.per_symbol_coverage
                       if item.status in {"COMPLETE_WITH_FILINGS", "COMPLETE_NO_NEW_FILINGS"})
        result = ("FAILED" if definite == 0 and projection.per_symbol_coverage
                  else "PARTIAL" if projection.status == "PARTIAL" else "OK")
        with self._lock:
            self._state["attempts"] += 1
            self._state["last_attempt_at"] = now.isoformat()
            self._state["last_result"] = result
            self._state["last_status"] = projection.status
            self._state["last_duration_ms"] = duration
            if definite:
                self._state["last_success_at"] = now.isoformat()
                self._projection = projection
        return projection
