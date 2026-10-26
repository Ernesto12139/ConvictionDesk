"""Manual, isolated SEC filings projection. Reads never start a source scan."""
from copy import deepcopy
from datetime import timedelta
from threading import Lock
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException, Request

from .domain import utc
from .local_api_security import direct_local
from .tdb import content_hash


class FilingsService:
    """Bounded public-source collector, separate from retained Attention records."""

    SCOPE_LIMIT = 12
    REFRESH_INTERVAL = timedelta(seconds=30)

    def __init__(self, *, entries, clock, collect=None, observer=None):
        self.entries, self.clock = entries, clock
        self.collect, self.observer = collect, observer
        self._lock = Lock()
        self._cached = None
        self._scanned_scope = None
        self._last_attempt = None
        self._scanning = False

    def _scope(self, now):
        # Deliberately omit private exposure, account, quantity and owner-note fields.
        selected = {(e.symbol, e.state) for e in self.entries(now)
                    if e.state in {'HOLDING', 'WATCHING'}}
        ordered = sorted(selected, key=lambda row: (row[1] != 'HOLDING', row[0]))
        return ordered[:self.SCOPE_LIMIT], len(ordered)

    def snapshot(self):
        now = utc(self.clock())
        scope, count = self._scope(now)
        with self._lock:
            cached = deepcopy(self._cached) if self._scanned_scope == scope else None
        if cached is None:
            return {'schema_version': 'ivs-sec-v1', 'status': 'NOT_SCANNED',
                    'as_of': now.isoformat(), 'scan_as_of': None, 'run_id': None,
                    'filings': [], 'per_symbol_coverage': [], 'scope_count': count,
                    'scope_limit': self.SCOPE_LIMIT, 'model_calls': 0,
                    'limitations': ['尚未扫描当前持仓与关注范围；读取页面不会发起 SEC 请求。']}
        cached['served_at'] = now.isoformat()
        cached['scope_count'] = count
        cached['scope_limit'] = self.SCOPE_LIMIT
        return cached

    def refresh(self):
        now = utc(self.clock())
        scope, count = self._scope(now)
        started = monotonic()
        with self._lock:
            if self._scanning or self._last_attempt is not None and started - self._last_attempt < self.REFRESH_INTERVAL.total_seconds():
                raise HTTPException(429, detail='SEC_REFRESH_COOLDOWN')
            self._last_attempt = started
            self._scanning = True
            # Keep the previous answer readable while this scan is in flight.
        run_id = str(uuid4())
        status, reason = 'SOURCE_UNAVAILABLE', 'SEC_SOURCE_NOT_CONFIGURED'
        result = {'filings': [], 'per_symbol_coverage': []}
        try:
            if not scope:
                status, reason = 'NOT_SCANNED', 'NO_SCOPE_TO_SCAN'
            elif self.collect is not None:
                result = self.collect([SimpleNamespace(symbol=symbol, state=kind)
                                       for symbol, kind in scope], as_of=now)
                status, reason = result['status'], None
        except Exception:
            # Provider error prose may contain request/private material; never expose it.
            result = {'filings': [], 'per_symbol_coverage': []}
            status, reason = 'SOURCE_UNAVAILABLE', 'SEC_COLLECTION_FAILED'
        limitations = list(result.get('limitations', []))
        if count > self.SCOPE_LIMIT:
            if status in {'COMPLETE_WITH_FILINGS', 'COMPLETE_NO_NEW_FILINGS'}:
                status = 'PARTIAL'
            limitations.append(f'本次仅覆盖前 {self.SCOPE_LIMIT} 个标的，其余尚未扫描。')
        cached = {**result, 'schema_version': 'ivs-sec-v1', 'status': status,
                  'as_of': now.isoformat(), 'scan_as_of': now.isoformat(),
                  'run_id': run_id, 'failure_reason': reason, 'model_calls': 0,
                  'limitations': limitations,
                  'latency_ms': round((monotonic() - started) * 1000)}
        with self._lock:
            if (status not in {'COMPLETE_WITH_FILINGS', 'COMPLETE_NO_NEW_FILINGS', 'PARTIAL'}
                    and self._cached is not None and self._scanned_scope == scope):
                # Preserve only this scope's last known answer, with its original
                # evidence cutoff. A failed refresh never proves fresh coverage.
                for key in ('filings', 'per_symbol_coverage', 'as_of', 'scan_as_of'):
                    cached[key] = deepcopy(self._cached.get(key))
                cached['stale'] = True
            self._cached, self._scanned_scope = deepcopy(cached), scope
            self._scanning = False
        if self.observer:
            self.observer({'run_id': run_id, 'source_identity': 'SEC_EDGAR_SUBMISSIONS',
                           'as_of': now.isoformat(),
                           'scope': [{'symbol': symbol, 'kind': kind} for symbol, kind in scope],
                           'filings': [{key: row.get(key) for key in (
                               'evidence_id', 'symbol', 'accession', 'accepted_at',
                               'time_precision', 'source_url', 'rights')}
                               for row in cached['filings']],
                           'coverage': cached.get('per_symbol_coverage', []),
                           'tool_choice': 'sec.recent_filings', 'route': 'MANUAL_SEC_SCAN',
                           'model_identity': None, 'latency_ms': cached['latency_ms'],
                           'tokens': None, 'cost': None, 'verdict': status,
                           'failure_reason': reason})
        return self.snapshot()

    def lookup(self, evidence_id):
        result = self.snapshot()
        if result.get('stale'):
            raise HTTPException(409, detail='SEC_FILING_CONTEXT_STALE')
        row = next((row for row in result['filings']
                    if row.get('evidence_id') == evidence_id), None)
        if row is None:
            raise HTTPException(404, detail='SEC_FILING_CONTEXT_UNAVAILABLE')
        return deepcopy(row)

    def evidence(self, evidence_id, cutoff):
        """Only public metadata can be pinned into a research task, never scope data."""
        from datetime import datetime
        from urllib.parse import urlsplit
        import re
        from .domain import EvidenceItem

        row = self.lookup(evidence_id)
        available = utc(datetime.fromisoformat(row['available_at'].replace('Z', '+00:00')))
        retrieved = utc(datetime.fromisoformat(row['provenance']['retrieved_at'].replace('Z', '+00:00')))
        target = urlsplit(row['source_url'])
        cik, accession = row['cik'], row['accession']
        if (available > cutoff or row['rights']['model_forwarding'] != 'GRANTED'
                or not re.fullmatch(r'\d{10}', cik)
                or not re.fullmatch(r'\d{10}-\d{2}-\d{6}', accession)
                or target.scheme != 'https' or target.hostname != 'www.sec.gov'
                or target.netloc != 'www.sec.gov' or target.query or target.fragment
                or not target.path.startswith(f'/Archives/edgar/data/{int(cik)}/{accession.replace("-", "")}/')):
            raise HTTPException(409, detail='SEC_FILING_CONTEXT_INVALID')
        metadata = {key: row[key] for key in ('cik', 'accession', 'form', 'items',
                    'accepted_at', 'time_precision', 'available_at', 'source_url')}
        metadata['claim_scope'] = 'PUBLIC_DISCLOSURE_METADATA_ONLY_NOT_INVESTMENT_IMPACT'
        return EvidenceItem(evidence_id=evidence_id, evidence_type='sec_filing',
                            provider='sec_edgar', symbol=row['symbol'],
                            title=f"{row['symbol']} {row['form']} public filing metadata",
                            source_uri=row['source_url'], source_record_id=accession,
                            published_at=available, as_of=available,
                            retrieved_at=max(retrieved, available),
                            content_hash=content_hash(metadata), structured_data=metadata)


def register_filings(app, service):
    app.state.ivs_filings_service = service

    @app.get('/api/today/filings')
    def filings():
        return service.snapshot()

    @app.get('/api/today/filings/{evidence_id}')
    def filing(evidence_id: str):
        return service.lookup(evidence_id)

    @app.post('/api/today/filings/refresh')
    def refresh(request: Request):
        if not direct_local(request):
            raise HTTPException(403, detail='DIRECT_LOOPBACK_REQUIRED')
        expected = f'{request.url.scheme}://{request.url.netloc}'
        if request.headers.get('origin', expected) != expected or request.headers.get('sec-fetch-site') in {'cross-site', 'same-site'}:
            raise HTTPException(403, detail='ORIGIN_REJECTED')
        if request.headers.get('content-type', '').split(';')[0] != 'application/json':
            raise HTTPException(415, detail='JSON_REQUIRED')
        return service.refresh()

    # Preserve the real queue response exactly; filings have their own projection.
    queue = next((r.endpoint for r in app.routes
                  if getattr(r, 'path', None) == '/api/attention/queue'), None)
    if queue is not None:
        app.add_api_route('/api/today/queue', queue, methods=['GET'])
