"""Keyless portfolio priority on the existing Console, research store and Decimal engine.

Only packaged synthetic portfolios and dated PUBLIC_SEC captures are composed here.
The ranking is committed cost, not current NAV, alpha, sizing or investment advice.
"""
from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from fastapi import HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import Field

from .agent import MockLLMClient
from .domain import EvidenceItem, FrozenModel, PriceObservation
from .llm import LLMError
from .news import NewsResult
from .portfolio_entry import PortfolioEntry
from .portfolio_whatif import plain
from .provider import MarketDataResult, ProviderError
from .sec import SECSearchResult, filing_metadata_hash
from .tdb import TDBError, content_hash

ASSET = Path(__file__).with_name('demo_assets') / 'portfolio-priority-v1.json'
DATASET_SHA256 = 'fb6de22fcacf9772f1528b0193d342ff1812040842d8bbfd429d00dceca03d67'
MARKER = 'PORTFOLIO_PRIORITY_SYNTHETIC_ONLY.json'
TOOL_REASONS = {
    'portfolio.get_snapshot': 'Bind this task to the immutable synthetic snapshot and selected account.',
    'market.get_eod': 'Read declared synthetic marks; missing real quotes remain unknown.',
    'portfolio.compute_analytics': 'Calculate the selected account context with Python Decimal.',
    'thesis.get': 'Check for a stored prior Thesis instead of inventing one.',
    'decision.get_previous': 'Look for a qualified prior decision; absence is explicit.',
    'sec.search': 'Read bounded historical PUBLIC_SEC metadata (Fast limit 2; Deep limit 4).',
    'sec.fundamentals': 'Deep only: investigate comparable revenue and cash-flow facts.',
    'valuation.compute': 'Calculate only metrics supported by available inputs; preserve unknowns.',
}


def load_dataset():
    raw = ASSET.read_bytes().replace(b'\r\n', b'\n')
    if hashlib.sha256(raw).hexdigest() != DATASET_SHA256:
        raise ValueError('PACKAGED_DATASET_INTEGRITY_FAILED')
    return json.loads(raw)


def instant(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return stamp


def metadata(capture, cutoff):
    """Verify identity, metadata hash and independent Accepted receipt before use."""
    from .sec_filings import CapturedFilingsSource
    if instant(capture['captured_at']) > cutoff:
        return []
    for row in capture['rows']:
        expected = filing_metadata_hash(accession=row['accession'], form=row['form'],
            acceptance=row.get('raw_submission_acceptance') or row['acceptance'],
            filing_date=row.get('filing_date'), items=','.join(row.get('items') or []),
            primary_document=row.get('primary_document'))
        if expected != row['raw_metadata_hash']:
            raise ValueError('SEC_METADATA_INTEGRITY_FAILED')
    result = CapturedFilingsSource(capture).recent_filings(capture['issuer']['symbol'], as_of=cutoff)
    valid = []
    for row in result.rows:
        if row.form not in {'10-Q', '10-K', '8-K'} or not row.accepted_at:
            continue
        if row.accepted_at > cutoff:
            continue
        valid.append(row)
    return valid


def financial_packet(data, cutoff):
    """Exactly comparable reported YTD facts, never a quarter/YTD or unit blend."""
    capture = data['financial_capture']
    symbol, cik = capture['symbol'], capture['cik']
    issuer = next(c for c in data['captures'] if c['issuer']['symbol'] == symbol)
    if issuer['issuer']['cik'] != cik or issuer['issuer']['tickers'] != [symbol]:
        raise ValueError('FINANCIAL_SECURITY_IDENTITY_MISMATCH')
    if capture['rights'] != 'PUBLIC_SEC' or capture['source_uri'] != f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json':
        raise ValueError('FINANCIAL_SOURCE_RIGHTS_MISMATCH')
    captured = instant(capture['captured_at'])
    if captured > cutoff:
        raise ValueError('FINANCIAL_CAPTURE_AFTER_CUTOFF')
    rows = metadata(issuer, cutoff)
    eligible = {r.accession: r for r in rows}
    facts = capture['facts']
    if len(facts) != 4:
        raise ValueError('EXACT_COMPARABLE_FACTS_REQUIRED')
    accessions = {f['accession_number'] for f in facts}
    if len(accessions) != 1 or not accessions <= eligible.keys():
        raise ValueError('FINANCIAL_ACCESSION_MISMATCH')
    filing = eligible[next(iter(accessions))]
    if captured < filing.accepted_at:
        raise ValueError('FINANCIAL_CAPTURE_PREDATES_ACCEPTANCE')
    groups = {}
    for fact in facts:
        value = Decimal(fact['value'])
        if (not value.is_finite() or fact['unit'] != 'USD'
                or fact['scope'] != 'ENTIRE_FILING_ENTITY'
                or instant(fact['available_at']) != filing.accepted_at
                or fact['start'] > fact['end'] or fact['end'] > cutoff.date().isoformat()):
            raise ValueError('FINANCIAL_PERIOD_UNIT_OR_PIT_MISMATCH')
        groups.setdefault(fact['metric'], []).append(fact)
    if set(groups) != {'revenue', 'operating_cash_flow'}:
        raise ValueError('COMPARABLE_METRICS_REQUIRED')
    periods = []
    comparison = {}
    with localcontext() as ctx:
        ctx.prec = 80
        for metric, group in sorted(groups.items()):
            if len(group) != 2:
                raise ValueError('COMPARABLE_PAIR_REQUIRED')
            previous, current = sorted(group, key=lambda f: f['start'])
            # Same calendar duration in adjacent years, both from the same filing.
            a, b = instant(previous['start'] + 'T00:00:00+00:00'), instant(current['start'] + 'T00:00:00+00:00')
            if (b.year != a.year + 1 or previous['start'][5:] != current['start'][5:]
                    or previous['end'][5:] != current['end'][5:] or previous['start'] == previous['end']):
                raise ValueError('COMPARABLE_PERIOD_REQUIRED')
            periods.append(tuple((f['start'], f['end']) for f in (previous, current)))
            prior, present = Decimal(previous['value']), Decimal(current['value'])
            if prior <= 0:
                raise ValueError('POSITIVE_COMPARISON_BASE_REQUIRED')
            comparison[metric] = {'previous': plain(prior), 'current': plain(present),
                'change': plain(present - prior),
                'yoy_pct': plain(((present / prior - 1) * 100).quantize(Decimal('.01')))}
    if periods[0] != periods[1]:
        raise ValueError('CROSS_METRIC_PERIOD_MISMATCH')
    comparison['relation'] = ('REVENUE_GROWTH_FASTER_THAN_OCF'
        if Decimal(comparison['revenue']['yoy_pct']) > Decimal(comparison['operating_cash_flow']['yoy_pct'])
        else 'REVENUE_GROWTH_NOT_FASTER_THAN_OCF')
    payload = {'symbol': symbol, 'cik': cik, 'rights': 'PUBLIC_SEC',
        'source_kind': 'FROZEN_SEC_XBRL_YTD', 'metrics': facts, 'comparison': comparison,
        'accepted_at': filing.accepted_at.isoformat(), 'accession': filing.accession,
        'issuer_listing_identity': issuer['issuer'], 'raw_source_sha256': capture['raw_source_sha256'],
        'capture_as_of': capture['captured_at'], 'pit_cutoff': cutoff.isoformat(),
        'limitations': ['Historical YTD company facts, not a current quote or a new live capture.',
                       'No confirmed prior Thesis; no automatic HOLD or valuation conclusion.']}
    digest = content_hash(payload)
    return EvidenceItem(evidence_id='E' + digest[7:23], evidence_type='sec_filing',
        provider='sec_edgar', symbol=symbol, title='PUBLIC_SEC 同期间收入与经营现金流',
        source_uri=capture['source_uri'], source_record_id=filing.accession,
        published_at=filing.accepted_at, as_of=filing.accepted_at, retrieved_at=captured,
        content_hash=digest, structured_data=payload,
        excerpt='公开申报的可比期间收入与经营现金流。收入增速快于经营现金流，需要继续核查现金转化。')


def priority(snapshot, data):
    """All holdings are ranked by committed cost; prices never silently fill gaps."""
    cutoff = instant(data['as_of'])
    captures = {c['issuer']['symbol']: c for c in data['captures']}
    prices = {s: Decimal(p) for s, p in data['marks']['prices'].items()}
    costs, holdings, accounts = {}, {}, []
    with localcontext() as ctx:
        ctx.prec = 80
        for account in snapshot.accounts:
            positions = []
            missing, values = [], []
            for p in snapshot.positions:
                if p.account_id != account.account_id:
                    continue
                cost = p.quantity * p.avg_cost
                costs[p.symbol] = costs.get(p.symbol, Decimal(0)) + cost
                holdings.setdefault(p.symbol, []).append(account.account_id)
                mark = prices.get(p.symbol)
                value = p.quantity * mark if mark is not None else None
                (missing if value is None else values).append(p.symbol if value is None else value)
                positions.append({'symbol': p.symbol, 'shares': plain(p.quantity),
                    'avg_cost': plain(p.avg_cost), 'committed_cost': plain(cost),
                    'synthetic_mark': plain(mark) if mark is not None else None,
                    'synthetic_value': plain(value) if value is not None else None,
                    'price_status': 'SYNTHETIC_NOT_A_QUOTE' if mark is not None else 'UNKNOWN'})
            nav = account.cash + sum(values, Decimal(0)) if not missing else None
            accounts.append({'account_id': account.account_id, 'name': account.name,
                'cash': plain(account.cash), 'cash_scope': 'THIS_ACCOUNT_ONLY', 'positions': positions,
                'synthetic_nav': plain(nav) if nav is not None else None,
                'current_nav': None, 'missing_price_symbols': sorted(missing)})
        candidates = []
        for rank, (symbol, cost) in enumerate(sorted(costs.items(), key=lambda pair: (-pair[1], pair[0])), 1):
            rows = metadata(captures[symbol], cutoff) if symbol in captures else []
            candidates.append({'rank': rank, 'symbol': symbol, 'committed_cost': plain(cost),
                'account_ids': sorted(holdings[symbol]),
                'mark_status': 'SYNTHETIC_NOT_A_QUOTE' if symbol in prices else 'UNKNOWN',
                'sec_status': 'FROZEN_METADATA' if rows else 'NOT_COVERED',
                'sec_captured_at': captures[symbol]['captured_at'] if symbol in captures else None,
                'financial_status': 'FROZEN_COMPARABLE_YTD' if symbol == data['financial_capture']['symbol'] else 'NOT_COVERED',
                'gaps': ['CURRENT_MARKET_PRICE', 'CURRENT_FULL_PORTFOLIO_RESEARCH', 'CONFIRMED_THESIS']
                    + ([] if rows else ['SEC_CAPTURE'])
                    + ([] if symbol == data['financial_capture']['symbol'] else ['COMPARABLE_FINANCIALS']),
                'reason': '按合成持仓累计投入成本降序；同额按证券代码排序。不是实时市值或收益预测。'})
    symbols = set(costs)
    return {'schema_version': 'portfolio-priority-v1', 'synthetic': True,
        'as_of': data['as_of'], 'snapshot_as_of': snapshot.as_of.isoformat(),
        'snapshot_id': snapshot.snapshot_id, 'snapshot_hash': snapshot.content_hash,
        'ranking_basis': 'COMMITTED_COST_DESC_SYMBOL_ASC', 'live_freshness': 'NOT_CURRENT_CONFIRMED',
        'marks_as_of': data['marks']['as_of'], 'accounts': accounts, 'candidates': candidates,
        'coverage': {'held_symbols': len(symbols), 'held_positions': len(snapshot.positions),
            'synthetic_priced_symbols': len(symbols & prices.keys()),
            'sec_metadata_symbols': sum(c['sec_status'] == 'FROZEN_METADATA' for c in candidates),
            'comparable_financial_symbols': 1, 'current_quote_symbols': 0,
            'missing_price_symbols': sorted(symbols - prices.keys())},
        'limitations': data['limitations'], 'model': 'SCRIPTED_NOT_LIVE', 'orders_created': 0}


class PublicSEC:
    name = 'packaged-public-sec'

    def __init__(self, data):
        from .sec_filings import CapturedFilingsSource
        self.data = data
        self.captures = {c['issuer']['symbol']: c for c in data['captures']}
        self.sources = {s: CapturedFilingsSource(c) for s, c in self.captures.items()}

    def recent_filings(self, symbol, *, as_of):
        if symbol not in self.sources:
            raise ProviderError('not_found', 'No packaged SEC capture')
        return self.sources[symbol].recent_filings(symbol, as_of=as_of)

    def search(self, symbol, query, forms, as_of, limit=5, **kwargs):
        capture = self.captures.get(symbol)
        result = []
        for row in metadata(capture, as_of) if capture else []:
            if row.form not in forms:
                continue
            payload = {'symbol': symbol, 'cik': capture['issuer']['cik'], 'accession': row.accession,
                'form': row.form, 'accepted_at': row.accepted_at.isoformat(), 'rights': 'PUBLIC_SEC',
                'period': 'UNKNOWN_METADATA_ONLY', 'unit': 'NOT_APPLICABLE_METADATA',
                'issuer_listing_identity': capture['issuer'], 'capture_as_of': capture['captured_at'],
                'raw_metadata_hash': row.raw_metadata_hash, 'pit_cutoff': as_of.isoformat()}
            digest = content_hash(payload)
            result.append(EvidenceItem(evidence_id='E' + digest[7:23], evidence_type='sec_filing',
                provider='sec_edgar', symbol=symbol, title='PUBLIC_SEC 历史申报元数据',
                source_uri=row.source_url, source_record_id=row.accession,
                published_at=row.accepted_at, as_of=row.accepted_at,
                retrieved_at=instant(capture['captured_at']), content_hash=digest,
                structured_data=payload, excerpt='历史公开申报元数据；未包含申报正文或经营结论。'))
        return SECSearchResult(cik=capture['issuer']['cik'] if capture else '',
            indexed_documents=0, evidence=result[:limit])

    fast_search = search
    targeted_search = search

    def fundamentals(self, symbol, as_of):
        return [financial_packet(self.data, as_of)] if symbol == self.data['financial_capture']['symbol'] else []


class SyntheticMarks:
    name = 'synthetic-marks-not-quotes'

    def __init__(self, data):
        self.data = data

    def retrieve_eod(self, symbols, cutoff):
        stamp = instant(self.data['marks']['as_of'])
        items, observations = [], []
        for symbol in sorted(set(symbols)):
            price = self.data['marks']['prices'].get(symbol)
            if price is None or stamp > cutoff:
                continue
            payload = {'symbol': symbol, 'price': price, 'synthetic': True, 'basis': 'SYNTHETIC_NOT_A_QUOTE'}
            digest = content_hash(payload)
            item = EvidenceItem(evidence_id='E' + digest[7:23], evidence_type='market_data',
                provider=self.name, symbol=symbol, title='合成标记，不是行情', as_of=stamp,
                published_at=stamp, retrieved_at=stamp, content_hash=digest, structured_data=payload)
            items.append(item)
            observations.append(PriceObservation(symbol=symbol, price=Decimal(price), as_of=stamp,
                provider=self.name, evidence_id=item.evidence_id))
        return MarketDataResult(observations=observations, evidence=items)


class NoNews:
    name = 'no-news-no-egress'

    def search(self, *args, **kwargs):
        return NewsResult(evidence=[], coverage_complete=False)


class ScriptedResearch(MockLLMClient):
    provider = 'scripted-local'
    model_name = 'SCRIPTED_PORTFOLIO_PRIORITY_NOT_LIVE'

    async def complete_structured(self, request):
        if request.purpose == 'research.intent':
            symbols = request.facts['explicit_literals']['symbols']
            return request.response_model.model_validate(dict(objective='REVIEW_THESIS',
                focus_symbol=symbols[0] if symbols else request.facts.get('previous_focus'), clarification=None,
                sec_query='operating cash flow revenue comparable period', use_news=False, use_sec=True))
        if request.purpose not in {'research.assess', 'research.reassess'}:
            raise LLMError('configuration', 'Unsupported scripted purpose')
        public = [s['evidence'] for s in request.facts['sources'] if s['evidence']['evidence_type'] == 'sec_filing']
        financial = next((e for e in public if e['structured_data'].get('source_kind') == 'FROZEN_SEC_XBRL_YTD'), None)
        ids = [e['evidence_id'] for e in ([financial] if financial else public[:1])]
        def statement(text, identity='AI_DRAFT'):
            return {'text': text, 'identity': identity, 'evidence_ids': ids}
        return request.response_model.model_validate(dict(disposition='RESEARCH_ONLY',
            core_judgment=statement('先核查现金转化与账户集中度；历史证据不足以形成当前交易方向。' if financial
                                    else '先核查公开申报与账户投入成本；元数据不能证明经营改善。'),
            key_reasons=[statement('已补读同一申报中收入与经营现金流的可比期间。' if financial
                                   else '已定位可追溯的历史公开申报。')],
            support=[statement('报告收入与经营现金流均较可比前期增加。')] if financial else [],
            counterevidence=[statement('收入增速快于经营现金流，现金转化仍需核查。' if financial
                                       else '尚无可比经营事实；申报存在不等于投资逻辑成立。', 'UNKNOWN' if not financial else 'AI_DRAFT')],
            action_conditions=[statement('缺少当前行情和确认过的 Thesis，保持 UNDETERMINED；不自动视为持有。')],
            next_checks=[statement('下一份申报发布后，先核对报告期、币种和股本；可比收入与经营现金流若继续背离，再重开现金转化核查。')],
            supplemental_query=None))


class ScriptedProfiles:
    defaults = {'FAST': 'priority-fast', 'DEEP': 'priority-deep'}

    def __init__(self, oracle):
        self.oracle = oracle

    def describe(self):
        return [dict(profile_id='priority-' + mode.lower(), mode=mode, provider=self.oracle.provider,
            model=self.oracle.model_name, available=True, max_output_tokens=3072 if mode == 'FAST' else 8192,
            timeout_seconds=45 if mode == 'FAST' else 90) for mode in ('FAST', 'DEEP')]

    def resolve(self, profile_id=None, mode=None):
        selected = profile_id or self.defaults[mode or 'DEEP']
        item = next((p for p in self.describe() if p['profile_id'] == selected), None)
        if item is None or mode and item['mode'] != mode:
            raise TDBError('RESEARCH_MODEL_PROFILE_INVALID', 422)
        return SimpleNamespace(**item)

    def client(self, profile_id):
        self.resolve(profile_id)
        return self.oracle


class Selection(FrozenModel):
    symbol: str = Field(pattern=r'^[A-Z][A-Z0-9.\-]{0,14}$')
    account_id: str = Field(min_length=1, max_length=100)
    mode: Literal['FAST', 'DEEP'] = 'FAST'
    task_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)


def build_app(root: Path | str, *, public_navigation=False):
    """Opt-in local assembly; never reads env secrets, Owner state or default SQLite."""
    from .persistence import Store
    from .research_task import ResearchRequest
    from .workbench_trace import install_workbench_trace, workbench_code_identity
    root = Path(root).absolute()
    for part in (root, *root.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError('SYNTHETIC_ROOT_LINK_REFUSED')
    data = load_dataset()
    identity = {'schema_version': data['schema_version'], 'dataset_hash': content_hash(data), 'synthetic': True}
    marker = root / MARKER
    database = root / 'portfolio-priority.sqlite3'
    if root.exists() and any(root.iterdir()):
        if not marker.is_file() or json.loads(marker.read_text(encoding='utf-8')) != identity:
            raise ValueError('SYNTHETIC_ROOT_NOT_OWNED')
    trace_dir = root / 'traces'
    for path in (marker, database, root / 'bootstrap.sqlite3', trace_dir,
                 *(trace_dir.glob('*') if trace_dir.is_dir() and not trace_dir.is_symlink() and not trace_dir.is_junction() else ())):
        if path.is_symlink() or path.is_junction() or path.is_file() and path.stat().st_nlink != 1:
            raise ValueError('SYNTHETIC_FILE_LINK_REFUSED')
    cutoff = instant(data['as_of'])
    financial_packet(data, cutoff)  # Fail before any state creation on invalid source/PIT.
    entry = PortfolioEntry(accounts=data['accounts'], positions=data['positions'])
    snapshot = entry.snapshot(instant(data['snapshot_as_of']))
    snapshot = snapshot.model_copy(update={'snapshot_id': 'SYN-' + identity['dataset_hash'][7:31]})
    root.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text(json.dumps(identity), encoding='utf-8')
    # main's legacy module-level app otherwise opens the cwd's default Owner DB
    # on import. Bootstrap it with an empty environment and our own synthetic file.
    # The explicit factory below receives all actual providers and no env secrets.
    import os
    from unittest.mock import patch
    with patch.dict(os.environ, {'MIA_DB_PATH': str(root / 'bootstrap.sqlite3')}, clear=True):
        from .main import create_app
    store = Store(database)
    current = store.get_snapshot()
    if current is None:
        store.add_snapshot(snapshot)
    elif current.model_dump(mode='json') != snapshot.model_dump(mode='json'):
        raise ValueError('SYNTHETIC_SNAPSHOT_MISMATCH')
    oracle = ScriptedResearch()
    app = create_app(database, market_provider=SyntheticMarks(data), sec_provider=PublicSEC(data),
        news_provider=NoNews(), llm_client=oracle, clock=lambda: cutoff,
        model_profiles=ScriptedProfiles(oracle), environment={'MIA_RESEARCH_STRATEGY': 'BASELINE_ONLY'})
    app.state.portfolio_priority = priority(snapshot, data)
    install_workbench_trace(app, trace_dir, workbench_code_identity(), 'KEYLESS_SCRIPTED')
    research_endpoint = next(r.endpoint for r in app.routes
        if getattr(r, 'path', '') == '/api/research/tasks' and 'POST' in getattr(r, 'methods', set()))
    original = next(r for r in app.routes if getattr(r, 'path', '') == '/')
    app.router.routes.remove(original)

    @app.get('/', include_in_schema=False)
    def console():
        html = (Path(__file__).with_name('static') / 'index.html').read_text(encoding='utf-8')
        if public_navigation:
            html = html.replace('<nav>', '<nav><a href="/public-replay">返回只读示例</a>', 1)
        # Existing Console shell and research reader, with unavailable creation and
        # provider surfaces removed in this opt-in composition only.
        for script in ('providers', 'attention', 'today', 'ivs-filings', 'portfolio-editor', 'portfolio-csv'):
            html = re.sub(r'\s*<script src="/static/' + script + r'\.js[^\"]*" defer></script>', '', html)
        html = html.replace('<body ', '<body data-portfolio-priority="1" ', 1)
        html = html.replace('href="#attention" class="active">研究工作台',
                            'href="#portfolio-priority" class="active">研究优先级', 1)
        html = html.replace('</head>', '<link rel="stylesheet" href="/static/portfolio-priority.css">'
            '<script src="/static/portfolio-priority.js" defer></script></head>')
        html = html.replace('<main>', '<main><section id="portfolio-priority" aria-label="组合优先研究与资金效率" tabindex="-1">'
            '<p role="status">正在读取合成组合旅程…</p></section>', 1)
        return HTMLResponse(html)

    @app.get('/api/portfolio-priority')
    def overview():
        return app.state.portfolio_priority

    @app.post('/api/portfolio-priority/research', status_code=202)
    async def research(selection: Selection):
        held = any(p.symbol == selection.symbol and p.account_id == selection.account_id for p in snapshot.positions)
        if not held:
            raise HTTPException(422, detail='SECURITY_NOT_HELD_IN_SELECTED_ACCOUNT')
        if selection.symbol not in app.state.research_service.agent.sec.captures:
            raise HTTPException(422, detail='PUBLIC_SEC_NOT_COVERED')
        if selection.task_id:
            previous = app.state.research_service.tasks.get(selection.task_id)
            if previous.account_id != selection.account_id or previous.focus_symbol != selection.symbol:
                raise HTTPException(422, detail='SAVED_TASK_SCOPE_MISMATCH')
            if selection.expected_revision is None:
                raise HTTPException(422, detail='EXPECTED_REVISION_REQUIRED')
        elif selection.mode != 'FAST':
            raise HTTPException(422, detail='START_WITH_FAST')
        return await research_endpoint(ResearchRequest(question=selection.symbol + ' 核查持仓投资逻辑与现金转化',
            account_id=selection.account_id, snapshot_id=snapshot.snapshot_id, mode=selection.mode,
            task_id=selection.task_id, expected_revision=selection.expected_revision, execution_strategy='BASELINE'))

    @app.get('/api/portfolio-priority/tasks/{task_id}/delta')
    def delta(task_id: str, revision: int | None = Query(default=None, ge=1)):
        tasks = app.state.research_service.tasks
        task = tasks.revision(task_id, revision) if revision else tasks.get(task_id)
        first = tasks.revision(task_id, 1)
        old = {s.evidence.evidence_id for s in first.sources if s.evidence.evidence_type == 'sec_filing'}
        added = [s.evidence for s in task.sources if s.evidence.evidence_type == 'sec_filing' and s.evidence.evidence_id not in old]
        return {'task_id': task_id, 'revision': task.revision, 'account_id': task.account_id,
            'model': 'SCRIPTED_NOT_LIVE', 'prior_thesis': 'NO_CONFIRMED_PRIOR_THESIS',
            'prior_decision': 'NO_PRIOR_VERSION' if task.revision == 1 else 'SAME_TASK_FAST_REVISION',
            'information_gain': ('NEW_PUBLIC_SEC_FACTS' if any(e.structured_data.get('source_kind') == 'FROZEN_SEC_XBRL_YTD' for e in added)
                else 'NEW_METADATA_ONLY' if added else 'NO_NEW_EVIDENCE'),
            'new_evidence': [e.model_dump(mode='json') for e in added],
            'first_revision_hash': content_hash(first.model_dump(mode='json')),
            'current_revision_hash': content_hash(task.model_dump(mode='json')),
            'tool_selection': [{'sequence': c.sequence, 'tool': c.name, 'status': c.status,
                'reason': TOOL_REASONS.get(c.name, 'Existing typed research prerequisite; see saved arguments and outputs.')}
                for c in task.tool_calls],
            'next_check': [s.text for s in task.assessment.next_checks] if task.assessment else [],
            'decision_status': task.action_decision.decision_status if task.action_decision else 'UNDETERMINED'}

    @app.middleware('http')
    async def boundary(request, call_next):
        path = request.url.path
        reading = request.method in {'GET', 'HEAD'} and (
            path in {'/', '/health', '/api/portfolio-priority', '/api/console/status',
                '/api/console/portfolio-overview', '/api/portfolio/current', '/api/research/model-profiles',
                '/api/research-universe', '/api/attention/queue', '/api/today/filings', '/api/market/pulse', '/api/market/live'}
            or re.fullmatch(r'/static/[\w-]+\.(?:css|js|svg)', path)
            or re.fullmatch(r'/api/research/tasks(?:/[^/]+(?:/(?:revisions(?:/[1-9][0-9]*)?|observation|what-if|execution-traces|valuation))?)?', path)
            or re.fullmatch(r'/api/portfolio-priority/tasks/[^/]+/delta', path)
            or re.fullmatch(r'/api/execution-traces/[a-f0-9-]+', path))
        writing = request.method == 'POST' and (path == '/api/portfolio-priority/research'
            or re.fullmatch(r'/api/research/tasks/[^/]+/what-if', path))
        if request.method not in {'GET', 'HEAD'}:
            from .local_api_security import direct_local
            origin = request.headers.get('origin')
            if not direct_local(request) or origin and origin != str(request.base_url).rstrip('/'):
                return JSONResponse(status_code=403, content={'detail': 'DIRECT_LOOPBACK_ORIGIN_REQUIRED'})
        if reading or writing:
            response = await call_next(request)
        else:
            response = JSONResponse(status_code=403, content={'detail': 'PUBLIC_PRIORITY_ROUTE_DISABLED'})
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-MIA-Mode'] = 'SYNTHETIC; PUBLIC_SEC_FROZEN; SCRIPTED_NOT_LIVE'
        return response
    return app


def main(argv=None):
    import argparse
    import socket
    import uvicorn
    parser = argparse.ArgumentParser(description='Keyless Portfolio Priority & Capital Efficiency Journey')
    parser.add_argument('--root', type=Path, required=True, help='Empty dedicated synthetic state directory')
    parser.add_argument('--port', type=int, default=8793)
    args = parser.parse_args(argv)
    if args.port == 8000 or not 1024 <= args.port <= 65535:
        raise SystemExit('PORT_REFUSED: choose a non-8000 loopback port')
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', args.port)) == 0:
            raise SystemExit('PORT_IN_USE: existing listener preserved')
    uvicorn.run(build_app(args.root), host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
