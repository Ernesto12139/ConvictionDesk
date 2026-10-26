"""Today coverage honesty: what the last real scan actually covered, and when.

The Owner's complaint was concrete: on 23 October the page still showed 17 October
reports. That is not a novelty-classification problem; it is the absence of a
first-class "how old is this data and what did we actually check" contract. This
module computes it from immutable scan rows only, with SQL aggregates so a
thousand-row backlog does not turn the page into a queue console.

Nothing here invents a timestamp. A missing scan is NEVER, not "just now".
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .domain import utc
from .market_evidence import NEWS_MAX_AGE

FRESH_HOURS = 6.0
STALE_HOURS = 24.0
BACKLOG_BOUNDED = 40
UNCOVERED_SAMPLE = 12

STATE_MEANING = {
    'FRESH': '最近一次成功扫描在六小时内。',
    'STALE': '最近一次成功扫描超过六小时；今日变化可能已经发生但未被检索。',
    'OLD': '最近一次成功扫描超过一天；页面上的材料不能当作今日事件。',
    'NEVER': '没有成功扫描记录；页面没有任何今日覆盖。',
}


def scan_state(hours: float | None) -> str:
    if hours is None:
        return 'NEVER'
    if hours <= FRESH_HOURS:
        return 'FRESH'
    if hours <= STALE_HOURS:
        return 'STALE'
    return 'OLD'


def _iso(value) -> str | None:
    return value if isinstance(value, str) else None


def _scan_row(db, *, completed_only: bool):
    # A scan that finished with disclosed limitations is still a successful scan;
    # treating only bare COMPLETED as success would report NEVER on most real runs.
    clause = ("WHERE status IN ('COMPLETED','COMPLETED_WITH_LIMITATIONS')"
              if completed_only else '')
    return db.execute(
        'SELECT scan_id,status,started_at FROM attention_scans '
        f'{clause} ORDER BY started_at DESC,rowid DESC LIMIT 1').fetchone()


def _backlog(db, active: set[str], now: datetime) -> dict:
    """Counts from SQL only. Never parses a thousand payloads on a poll."""
    window_start = (now - NEWS_MAX_AGE).isoformat()
    rows = db.execute(
        "SELECT symbol, COUNT(*) AS total, "
        "  MIN(json_extract(payload,'$.evidence[0].published_at')) AS oldest, "
        "  SUM(CASE WHEN json_extract(payload,'$.evidence[0].published_at') >= ? THEN 1 ELSE 0 END), "
        "  SUM(CASE WHEN json_extract(payload,'$.evidence[0].published_at') < ? "
        "        AND (julianday(json_extract(payload,'$.cutoff')) "
        "             - julianday(json_extract(payload,'$.evidence[0].published_at'))) <= 7 "
        "      THEN 1 ELSE 0 END) "
        "FROM attention_events WHERE screen IS NULL GROUP BY symbol",
        (window_start, window_start)).fetchall()
    total = current = expired_unscreened = 0
    oldest = None
    by_symbol = {}
    for symbol, count, first, current_count, expired_count in rows:
        if symbol not in active:
            continue
        row_current = current_count or 0
        row_expired = max(0, count - row_current)
        row_eligible = expired_count or 0
        by_symbol[symbol] = {'current': row_current,
                             'expired_unscreened': row_eligible,
                             'historical_other': max(0, row_expired - row_eligible)}
        total += count
        current += row_current
        expired_unscreened += row_eligible
        if first and (oldest is None or first < oldest):
            oldest = first
    expired = max(0, total - current)
    historical_other = max(0, expired - expired_unscreened)
    if total == 0:
        triage, sentence = 'CLEAR', '没有待筛材料。'
    elif total <= BACKLOG_BOUNDED:
        triage, sentence = 'BOUNDED', f'{total} 条待筛材料，可在一轮内处理。'
    else:
        triage, sentence = 'LARGE', (
            f'{total} 条待筛材料，其中 {expired} 条已超出七天窗口；'
            '这些不会自动占用今日名额，也不需要你逐条处理。')
    return {
        'pending_total': total,
        'pending_current': current,
        'pending_expired': expired,
        'pending_expired_eligible': expired_unscreened,
        'pending_historical_other': historical_other,
        'oldest_pending_at': oldest,
        'by_symbol': by_symbol,
        'triage': triage,
        'sentence': sentence,
        'bound': BACKLOG_BOUNDED,
        'basis': 'SQL_AGGREGATE',
        'meaning': ('Backlog is a system property, not a user task. Expired receipts stay '
                    'stored and visible on request; they never fill today\'s slots.'),
    }


def coverage_projection(store, research, entries, *, now: datetime) -> dict:
    """One read-only projection of scan freshness, coverage and backlog."""
    now = utc(now)
    active = [entry for entry in entries if entry.state != 'ARCHIVED']
    symbols = {entry.symbol for entry in active}
    with store._connect() as db:
        successful = _scan_row(db, completed_only=True)
        attempt = _scan_row(db, completed_only=False)
        backlog = _backlog(db, symbols, now)
    report = research.report(successful[0])['report'] if successful else None
    last_success = utc(datetime.fromisoformat(successful[2])) if successful else None
    hours = ((now - last_success).total_seconds() / 3600.0) if last_success else None
    state = scan_state(hours)

    symbol_coverage = (report or {}).get('symbol_coverage') or {}
    # Only a scan that actually returned a window counts as coverage; a provider
    # failure is an uncovered symbol, not a checked one.
    covered_status = {'COMPLETE_WINDOW', 'PARTIAL_SOURCE'}
    checked = [symbol for symbol in symbols
               if (symbol_coverage.get(symbol) or {}).get('status') in covered_status]
    uncovered = sorted(symbols - set(checked))
    per_symbol = {}
    for entry in active:
        row = symbol_coverage.get(entry.symbol) or {}
        per_symbol[entry.symbol] = {
            'scope': entry.state,
            'last_checked_at': last_success.isoformat() if last_success else None,
            'status': row.get('status', 'NOT_FETCHED_IN_THIS_SCAN'),
            'since': row.get('since'),
            'until': row.get('until'),
            'error': row.get('error'),
            'checked_through': entry.last_attention_cutoff.isoformat()
                if entry.last_attention_cutoff else None,
        }
    attempt_status = attempt[1] if attempt else 'NEVER'
    return {
        'state': state,
        'state_sentence': STATE_MEANING[state],
        'last_successful_scan_at': last_success.isoformat() if last_success else None,
        'hours_since_successful_scan': round(hours, 2) if hours is not None else None,
        'last_attempt_at': _iso(attempt[2]) if attempt else None,
        'last_attempt_status': attempt_status,
        'scan_result_status': (report or {}).get('result_status'),
        'checked_symbols': len(checked),
        'active_symbols': len(symbols),
        'coverage_ratio': round(len(checked) / len(symbols), 3) if symbols else None,
        'uncovered_symbols': uncovered[:UNCOVERED_SAMPLE],
        'uncovered_count': len(uncovered),
        'scan_window': ({'since': report.get('candidate_window'),
                         'until': report.get('query_cutoff')} if report else None),
        'per_symbol': per_symbol,
        'backlog': backlog,
        'meaning': ('Coverage is what the last successful scan actually requested and screened. '
                    'A stale scan cannot be repaired by a newer publication date on a card.'),
    }


def stale_gate(state: str) -> str | None:
    """Reason string when no card may be presented as a current change."""
    if state == 'OLD':
        return 'SCAN_STALE_OVER_24H'
    if state == 'NEVER':
        return 'NO_SUCCESSFUL_SCAN'
    return None


def age_hours(value: datetime | None, now: datetime) -> float | None:
    if not value:
        return None
    return round((utc(now) - utc(value)).total_seconds() / 3600.0, 2)


def window_start(now: datetime) -> datetime:
    return utc(now) - NEWS_MAX_AGE


def sample_json(payload: str) -> dict:
    """Small helper for tests and diagnostics; never used on a hot path."""
    return json.loads(payload)
