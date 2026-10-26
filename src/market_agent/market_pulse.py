"""Today's live Market Pulse and latest headlines; data providers only, no model.

Answers "what is moving and what was just reported" for holdings, the watch
list and the broad market. Quotes and headlines are fetched on demand from
Finnhub, cached briefly in memory and never written to the database, so a
pulse refresh is neither a research claim nor evidence for one. Arithmetic is
Decimal; a missing or stale price stays unknown. Grouping is a display aid for
reposts of one story, never an importance, truth or confidence score.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import threading
import time
import unicodedata
from urllib.parse import urlsplit

from zoneinfo import ZoneInfo

import httpx

from .clock import system_utc_now
from .domain import utc
from .provider import SYMBOL

FINNHUB = 'https://finnhub.io/api/v1'
EASTERN = ZoneInfo('America/New_York')
# These are the fund tickers that stand in for the indices. SPY is an ETF, never
# the S&P 500 index level; the label must not blur the two.
BENCHMARKS = (('SPY', '标普500（SPY 代理）'), ('QQQ', '纳斯达克100（QQQ 代理）'),
              ('IWM', '罗素2000（IWM 代理）'), ('DIA', '道琼斯（DIA 代理）'))
BENCHMARK_PROXY_OF = {'SPY': 'S&P 500 index', 'QQQ': 'Nasdaq-100 index',
                      'IWM': 'Russell 2000 index', 'DIA': 'Dow Jones Industrial Average'}
SECTORS = (('SMH', '半导体'), ('XLK', '科技'), ('XLC', '通信'), ('XLY', '可选消费'),
           ('XLF', '金融'), ('XLV', '医疗'), ('XLE', '能源'), ('XLU', '公用事业'))
# 5/20-day context comes from completed daily bars, one request per symbol, so the
# set is capped and cached far longer than a quote.
# Volatility and the long rate are the two macro series this desk actually uses.
# They are index values, not funds: ^VIX is a volatility index level and ^TNX is a
# yield index quoted in percent. When the plan excludes them the row still appears,
# with an explicit refusal, because a missing VIX must never read as zero.
MACRO = (('^VIX', '波动率指数 VIX'), ('^TNX', '10 年期美债收益率'))
MACRO_KIND = {'^VIX': 'VOLATILITY_INDEX', '^TNX': 'YIELD_INDEX'}
MACRO_UNIT = {'^VIX': 'INDEX_POINTS', '^TNX': 'PERCENT'}
RANGE_SYMBOL_LIMIT = 6
RANGE_TTL = timedelta(minutes=30)
RANGE_SESSIONS = 20
RANGE_DEADLINE_SECONDS = 6.0
# Finnhub free tier allows 60 calls/minute: 10 ETF + 30 holding quotes + 20 news.
MAX_HOLDINGS = 30
MAX_NEWS_SYMBOLS = 20
MAX_WATCHING = 8
HEADLINE_WINDOW = timedelta(hours=48)
QUOTE_TTL = timedelta(minutes=2)
NEWS_TTL = timedelta(minutes=10)
MIN_FORCED_REFRESH = timedelta(seconds=60)
STALE_QUOTE = timedelta(days=4)
_STOP = frozenset('the and with from after that this its are for has have will says said into amid over '
                  'stock stocks shares share why what how today could would than more just'.split())
_LEAD = re.compile(r'^(?:UPDATE\s*\d*|BREAKING|EXCLUSIVE|CORRECTION)\s*[-:—]\s*', re.I)


def _tokens(title):
    text = _LEAD.sub('', unicodedata.normalize('NFKC', title or '')).casefold()
    return {t for t in re.findall(r'[a-z][a-z0-9]{2,}', text) if t not in _STOP}


def _numbers(title):
    return set(re.findall(r'\d+(?:\.\d+)?%?', title or ''))


def _same_story(left, right):
    """Display-only repost grouping: close in time, near-identical headline words."""
    if abs((left['published_at'] - right['published_at']).total_seconds()) > 36 * 3600:
        return False
    a, b = _tokens(left['title']), _tokens(right['title'])
    if len(a) < 3 or len(b) < 3:
        return left['title'].casefold() == right['title'].casefold()
    numbers_left, numbers_right = _numbers(left['title']), _numbers(right['title'])
    if numbers_left and numbers_right and not numbers_left & numbers_right:
        return False
    return len(a & b) / len(a | b) >= 0.5


def _domain(uri):
    try:
        host = urlsplit(uri or '').hostname or ''
    except ValueError:
        return None
    parts = host.removeprefix('www.').split('.')
    return '.'.join(parts[-2:]) if len(parts) >= 2 else None


def group_headlines(items):
    """Newest-first representative per story, with the reposts kept findable."""
    items = sorted(items, key=lambda i: i['published_at'], reverse=True)
    groups = []
    for item in items:
        home = next((g for g in groups if _same_story(g['lead'], item)), None)
        if home is None:
            groups.append({'lead': item, 'members': [item]})
        else:
            home['members'].append(item)
    output = []
    for group in groups:
        lead, members = group['lead'], group['members']
        outlets = sorted({m['source'] for m in members if m['source']})
        output.append({**_public(lead), 'reports': len(members), 'outlets': outlets[:6],
                       'earliest_at': min(m['published_at'] for m in members).isoformat(),
                       'identity': headline_identity(_public(lead)),
                       'reposts': [_public(m) for m in members[1:6]]})
    return output


def _public(item):
    return {'title': item['title'], 'summary': item['summary'], 'source': item['source'],
            'url': item['url'], 'domain': _domain(item['url']), 'symbol': item['symbol'],
            'published_at': item['published_at'].isoformat()}


def headline_identity(item):
    """Provenance for an unscreened headline, so it can be cited rather than retold.

    `screened` is False and stays False: nothing here has been judged material.
    """
    return {'kind': 'PROVIDER_HEADLINE', 'screened': False,
            'publisher': item.get('source') or None,
            'url': item.get('url'), 'published_at': item.get('published_at'),
            'digest': hashlib.sha256('|'.join([
                (item.get('title') or '').strip().casefold(),
                item.get('symbol') or '', item.get('published_at') or '']).encode('utf-8')
            ).hexdigest()[:16],
            'meaning': ('Publisher metadata and a content digest. Not a verified event, not '
                        'an importance score, and not permission to forward the text to a model.')}


SESSION_BASIS = 'WEEKDAY_EASTERN_CLOCK_NO_EXCHANGE_CALENDAR'


def market_session(now):
    """Which session the timestamp falls in, by the Eastern clock alone.

    No exchange calendar, no holiday table, no half-day knowledge: this answers
    "what time is it in New York" and says so, rather than claiming to know
    whether the exchange actually traded.
    """
    local = utc(now).astimezone(EASTERN)
    if local.weekday() >= 5:
        return 'CLOSED_WEEKEND'
    minute = local.hour * 60 + local.minute
    if 4 * 60 <= minute < 9 * 60 + 30:
        return 'PRE_MARKET'
    if 9 * 60 + 30 <= minute < 16 * 60:
        return 'REGULAR'
    if 16 * 60 <= minute < 20 * 60:
        return 'AFTER_HOURS'
    return 'CLOSED_OVERNIGHT'


def instrument_kind(symbol):
    if symbol in MACRO_KIND:
        return MACRO_KIND[symbol]
    if symbol in BENCHMARK_PROXY_OF:
        return 'INDEX_ETF_PROXY'
    if any(symbol == code for code, _ in SECTORS):
        return 'SECTOR_ETF'
    return 'EQUITY'


def delay_status(lag_seconds):
    """How old the provider's own timestamp is. Never a promise of real time."""
    if lag_seconds is None:
        return 'UNKNOWN'
    if lag_seconds <= 60:
        return 'UNDER_ONE_MINUTE'
    if lag_seconds <= 900:
        return 'UNDER_FIFTEEN_MINUTES'
    if lag_seconds <= 86400:
        return 'DELAYED_OVER_FIFTEEN_MINUTES'
    return 'MULTI_DAY'


def _pct_change(last, earlier):
    if not earlier or earlier <= 0:
        return None
    return (last / earlier - 1) * Decimal(100)


def range_projection(history, sessions=RANGE_SESSIONS):
    """5/20-day change and realized volatility from completed daily closes.

    Split-adjusted closes are used as given; no dividend adjustment, no gap
    filling. Fewer than 21 bars means the 20-day figures are UNKNOWN, never zero.
    """
    closes = [bar.close for bar in history.bars]
    out = {'provider': history.source, 'price_basis': history.price_basis,
           'sessions_available': len(closes), 'as_of_session': None,
           'change_5d_pct': None, 'change_20d_pct': None, 'realized_vol_20d_pct': None,
           'status': 'UNKNOWN', 'reason': None}
    if not closes:
        out['reason'] = 'NO_COMPLETED_SESSIONS'
        return out
    out['as_of_session'] = history.bars[-1].session_date.isoformat()
    if len(closes) < 6:
        out['reason'] = 'INSUFFICIENT_SESSIONS'
        return out
    out['change_5d_pct'] = str(_pct_change(closes[-1], closes[-6]).quantize(Decimal('0.01')))
    if len(closes) >= 21:
        out['change_20d_pct'] = str(_pct_change(closes[-1], closes[-21]).quantize(Decimal('0.01')))
        window = closes[-21:]
        returns = [(window[i] / window[i - 1]).ln() for i in range(1, len(window))]
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        out['realized_vol_20d_pct'] = str(
            (variance.sqrt() * Decimal(252).sqrt() * Decimal(100)).quantize(Decimal('0.01')))
    out['status'] = 'AVAILABLE' if out['change_20d_pct'] is not None else 'PARTIAL'
    return out


class RangeContext:
    """Bounded completed-daily-bar context, built lazily and cached.

    The Massive client is created on first use so app startup builds no TLS
    context. One request per symbol, at most RANGE_SYMBOL_LIMIT symbols, cached
    for RANGE_TTL. A missing key or a refused symbol is reported, never filled.
    """

    def __init__(self, api_key, store, *, clock=system_utc_now, provider=None):
        self._api_key = api_key or ''
        self._store = store
        self._clock = clock
        self._provider = provider
        self._cache = {}
        self._lock = threading.Lock()

    @property
    def status(self):
        if self._provider is not None:
            return 'AVAILABLE'
        return 'AVAILABLE' if self._api_key and self._store is not None else 'NOT_CONFIGURED'

    def _build(self):
        if self._provider is None:
            from .provider import MassiveEODProvider
            self._provider = MassiveEODProvider(self._api_key, self._store, clock=self._clock)
        return self._provider

    def summary(self, symbol, now):
        if self.status != 'AVAILABLE':
            return {'status': self.status, 'reason': 'PROVIDER_NOT_CONFIGURED',
                    'symbol': symbol, 'change_5d_pct': None, 'change_20d_pct': None,
                    'realized_vol_20d_pct': None}
        cached = self._cache.get(symbol)
        if cached and now - cached[0] < RANGE_TTL:
            return cached[1]
        try:
            history, _evidence = self._build().completed_history(symbol, now)
            result = range_projection(history)
        except Exception as exc:  # provider errors are answers, not crashes
            from .provider import ProviderError
            reason = getattr(exc, 'code', None) if isinstance(exc, ProviderError) else type(exc).__name__
            result = {'status': 'UNAVAILABLE', 'reason': str(reason).upper()[:40],
                      'symbol': symbol, 'change_5d_pct': None, 'change_20d_pct': None,
                      'realized_vol_20d_pct': None}
        result['symbol'] = symbol
        if result['status'] in {'AVAILABLE', 'PARTIAL'}:
            with self._lock:
                self._cache[symbol] = (now, result)
        return result


class MarketPulse:
    def __init__(self, api_key, universe, *, client=None, clock=system_utc_now, enabled=True,
                 range_source=None):
        self._api_key = api_key or ''
        self.universe = universe
        self._client = client  # created on first use; app startup builds no TLS context
        self.clock = clock
        self.enabled = enabled
        self._lock = threading.Lock()
        self._quotes = {}
        self._news = {}
        self._ranges = {}
        self._last_forced = None
        self.range_source = range_source
        self._refresh = {'attempts': 0, 'last_attempt_at': None, 'last_success_at': None,
                         'last_result': None, 'last_duration_ms': None,
                         'last_uncovered': [], 'last_forced': False}

    @property
    def client(self):
        if self._client is None:
            self._client = httpx.Client(timeout=8)
        return self._client

    @property
    def status(self):
        if not self.enabled:
            return 'OFFLINE_REPLAY'
        return 'AVAILABLE' if self._api_key else 'NOT_CONFIGURED'

    def _get(self, path, params):
        response = self.client.get(f'{FINNHUB}/{path}', params=params,
                                   headers={'X-Finnhub-Token': self._api_key})
        if response.status_code in (401, 403):
            raise PermissionError('NOT_ENTITLED')
        if response.status_code == 429:
            raise RuntimeError('RATE_LIMITED')
        if response.status_code != 200:
            raise RuntimeError('UNAVAILABLE')
        return json.loads(response.text, parse_float=Decimal)

    def quote(self, symbol, now):
        cached = self._quotes.get(symbol)
        if cached and now - cached[0] < QUOTE_TTL:
            return cached[1]
        result = {'symbol': symbol, 'status': 'UNAVAILABLE'}
        try:
            raw = self._get('quote', {'symbol': symbol})
            # Finnhub answers HTTP 200 with {"error": ...} for a symbol the plan
            # does not carry. That is an entitlement answer, not a missing price.
            if isinstance(raw, dict) and raw.get('error'):
                result['status'] = 'NOT_ENTITLED'
                result['detail'] = str(raw.get('error'))[:120]
            else:
                stamp = raw.get('t') if isinstance(raw, dict) else None
                price, previous = (Decimal(str(raw.get(k))) for k in ('c', 'pc'))
                if type(stamp) is not int or stamp <= 0 or not price.is_finite() or price <= 0:
                    result['status'] = 'NO_PRICE'
                else:
                    as_of = datetime.fromtimestamp(stamp, timezone.utc)
                    result = {'symbol': symbol,
                              'status': 'STALE' if now - as_of > STALE_QUOTE else 'AVAILABLE',
                              'price': str(price), 'as_of': as_of.isoformat(), 'provider': 'finnhub',
                              'basis': 'QUOTE_MAY_BE_DELAYED'}
                    if previous.is_finite() and previous > 0:
                        change = price - previous
                        result |= {'previous_close': str(previous), 'change': str(change),
                                   'change_pct': str((change / previous * 100).quantize(Decimal('0.01')))}
        except PermissionError:
            result['status'] = 'NOT_ENTITLED'
        except RuntimeError as exc:
            result['status'] = str(exc)
        except (httpx.HTTPError, ValueError, TypeError, InvalidOperation, OverflowError):
            result['status'] = 'UNAVAILABLE'
        result = self._contract(symbol, result, now)
        if result['status'] in {'AVAILABLE', 'STALE', 'NO_PRICE', 'NOT_ENTITLED'}:
            self._quotes[symbol] = (now, result)
        return result

    def _contract(self, symbol, result, now):
        """Every metric carries provenance, lag and entitlement, whatever happened.

        `entitlement` and `coverage_status` are separate on purpose: a refused
        symbol is fully explained, while a transport failure is simply unknown.
        """
        kind = instrument_kind(symbol)
        as_of = result.get('as_of')
        lag = None
        if as_of:
            try:
                lag = max(0, int((now - datetime.fromisoformat(as_of)).total_seconds()))
            except ValueError:
                lag = None
        status = result['status']
        definite = status in {'AVAILABLE', 'STALE', 'NO_PRICE', 'NOT_ENTITLED'}
        return result | {
            'instrument_kind': kind,
            'proxy_of': BENCHMARK_PROXY_OF.get(symbol),
            'is_index_level': symbol in MACRO_KIND,
            'unit': MACRO_UNIT.get(symbol, 'USD'),
            'market_session': market_session(now),
            'session_basis': SESSION_BASIS,
            'observed_at': now.isoformat(),
            'observed_lag_seconds': lag,
            'delay_status': delay_status(lag),
            'entitlement': 'REFUSED_BY_PLAN' if status == 'NOT_ENTITLED' else
                           'CONFIRMED_BY_DATED_TICK' if as_of else 'UNCONFIRMED',
            'coverage_status': 'AVAILABLE' if status == 'AVAILABLE' else
                               'STALE' if status == 'STALE' else
                               'NOT_ENTITLED' if status == 'NOT_ENTITLED' else
                               'NO_VALUE' if status == 'NO_PRICE' else 'UNKNOWN',
            'answer_kind': 'DEFINITE' if definite else 'UNKNOWN',
        }

    def _normalize(self, raw, symbol, since, now):
        items = []
        for record in raw if isinstance(raw, list) else []:
            if not isinstance(record, dict):
                continue
            stamp, title = record.get('datetime'), record.get('headline')
            if type(stamp) is not int or stamp <= 0 or not isinstance(title, str) or not title.strip():
                continue
            published = datetime.fromtimestamp(stamp, timezone.utc)
            if not since <= published <= now + timedelta(minutes=5):
                continue
            summary = record.get('summary') if isinstance(record.get('summary'), str) else ''
            items.append({'symbol': symbol, 'title': title.strip()[:240], 'summary': summary.strip()[:360],
                          'source': str(record.get('source') or '').strip()[:60],
                          'url': record.get('url') if isinstance(record.get('url'), str)
                          and record['url'].startswith('https://') else None,
                          'published_at': published})
        return items

    def news(self, symbol, now):
        key = symbol or ':market'
        cached = self._news.get(key)
        if cached and now - cached[0] < NEWS_TTL:
            return cached[1]
        since = now - HEADLINE_WINDOW
        try:
            if symbol:
                raw = self._get('company-news', {'symbol': symbol, 'from': since.date().isoformat(),
                                                 'to': now.date().isoformat()})
            else:
                raw = self._get('news', {'category': 'general'})
            result = {'status': 'AVAILABLE', 'items': self._normalize(raw, symbol, since, now)}
        except PermissionError:
            result = {'status': 'NOT_ENTITLED', 'items': []}
        except RuntimeError as exc:
            result = {'status': str(exc), 'items': []}
        except (httpx.HTTPError, ValueError, TypeError):
            result = {'status': 'UNAVAILABLE', 'items': []}
        if result['status'] == 'AVAILABLE':
            self._news[key] = (now, result)
        return result

    def snapshot(self, *, refresh=False):
        now = utc(self.clock())
        base = {'status': self.status, 'generated_at': now.isoformat(), 'provider': 'finnhub',
                'stored': False, 'model_calls': 0,
                'meaning': ('Live data-provider snapshot, not saved and not screened by a model. '
                            'Report counts group reposts of one story; they are not importance scores.')}
        entries = [e for e in self.universe(now) if e.state != 'ARCHIVED' and SYMBOL.fullmatch(e.symbol)]
        holdings = sorted((e for e in entries if e.state == 'HOLDING'),
                          key=lambda e: -(e.cost_exposure_pct or Decimal(0)))[:MAX_HOLDINGS]
        watching = sorted((e for e in entries if e.state != 'HOLDING'),
                          key=lambda e: (e.state != 'WATCHING', e.symbol))[:MAX_WATCHING]
        base['universe'] = {'holdings': len([e for e in entries if e.state == 'HOLDING']),
                            'watching': len([e for e in entries if e.state != 'HOLDING']),
                            'holding_limit': MAX_HOLDINGS, 'watching_limit': MAX_WATCHING,
                            'news_symbol_limit': MAX_NEWS_SYMBOLS}
        base['market_session'] = market_session(now)
        base['session_basis'] = SESSION_BASIS
        if self.status != 'AVAILABLE':
            return base | {'indices': [], 'sectors': [], 'holdings': [], 'scopes': {},
                           'macro': [], 'gaps': [], 'range_context': {'status': self.status},
                           'theme_scope': {'status': 'UNKNOWN', 'watched_sectors': [],
                                           'available_sectors': [c for c, _ in SECTORS],
                                           'basis': 'UNIVERSE_WATCHING_ENTRY_ON_A_SECTOR_ETF',
                                           'how_to_add': '先配置数据源，再在「研究范围」添加行业 ETF。',
                                           'meaning': '没有数据源时无法判断行业范围。'},
                           'refresh_state': self._refresh_state(now, None),
                           'budget': self._budget([], [])}

        started = time.perf_counter()
        forced = False
        with self._lock:
            if refresh and (self._last_forced is None or now - self._last_forced >= MIN_FORCED_REFRESH):
                self._last_forced = now
                forced = True
                self._quotes.clear()
                self._news.clear()
            quote_symbols = ([s for s, _ in BENCHMARKS + SECTORS + MACRO]
                             + [e.symbol for e in holdings])
            news_members = (holdings[:MAX_NEWS_SYMBOLS - len(watching)] if len(holdings) + len(watching) > MAX_NEWS_SYMBOLS
                            else holdings) + watching
            news_symbols = [e.symbol for e in news_members] + [None]
            with ThreadPoolExecutor(max_workers=4) as pool:
                quotes = dict(zip(quote_symbols, pool.map(lambda s: self.quote(s, now), quote_symbols)))
                news = dict(zip(news_symbols, pool.map(lambda s: self.news(s, now), news_symbols)))

        ranges, range_deadline = {}, time.perf_counter() + RANGE_DEADLINE_SECONDS
        if self.range_source is not None:
            for symbol, _label in (BENCHMARKS + SECTORS)[:RANGE_SYMBOL_LIMIT]:
                if time.perf_counter() > range_deadline:
                    # A slow history provider must not hold the whole page hostage.
                    ranges[symbol] = {'symbol': symbol, 'status': 'TRUNCATED_BY_DEADLINE',
                                      'change_5d_pct': None, 'change_20d_pct': None,
                                      'realized_vol_20d_pct': None}
                    continue
                ranges[symbol] = self.range_source.summary(symbol, now)

        def labelled(pairs):
            return [quotes[s] | {'label': label, 'range': ranges.get(s)} for s, label in pairs]

        def scope(members, label='MARKET'):
            skipped = [e.symbol for e in members if e.symbol not in news]
            members = [e for e in members if e.symbol in news]
            items = [i for e in members for i in news[e.symbol]['items']]
            # A refusal is a definite answer and a transport failure is not. Collapsing
            # them let the page say "no news in 48 hours" about a source it was never
            # allowed to read, which is the most misleading sentence this page can print.
            refused = sorted(e.symbol for e in members if news[e.symbol]['status'] == 'NOT_ENTITLED')
            failed = sorted(e.symbol for e in members
                            if news[e.symbol]['status'] not in {'AVAILABLE', 'NOT_ENTITLED'})
            checked = len(members)
            if not checked:
                coverage = 'NOT_CHECKED'
            elif not refused and not failed:
                coverage = 'AVAILABLE'
            elif not items and len(refused) == checked:
                coverage = 'REFUSED'
            elif not items and len(failed) == checked:
                coverage = 'UNKNOWN'
            else:
                coverage = 'PARTIAL'
            return {'symbols': [e.symbol for e in members], 'groups': group_headlines(items)[:30],
                    'unavailable_symbols': sorted(set(refused) | set(failed)),
                    'not_entitled_symbols': refused, 'failed_symbols': failed,
                    'coverage_status': coverage, 'checked_count': checked,
                    'not_checked_symbols': skipped, 'screened': False,
                    'meaning': ('Raw provider headlines in the last 48 hours, grouped only to '
                                'fold reposts. Not screened by a model and not an importance '
                                'claim. coverage_status REFUSED means the plan excludes the '
                                'source, not that the source was quiet.')}

        watched_codes = {e.symbol for e in entries if e.state != 'HOLDING'}
        watched_sectors = [code for code, _ in SECTORS if code in watched_codes]
        exposure = {e.symbol: e.cost_exposure_pct for e in holdings}
        holding_quotes = [quotes[e.symbol] | {'label': e.symbol,
                          'cost_exposure_pct': str(exposure[e.symbol])
                          if exposure[e.symbol] is not None else None,
                          'range': ranges.get(e.symbol)} for e in holdings]
        market = news[None]
        indices, sectors = labelled(BENCHMARKS), labelled(SECTORS)
        macro = labelled(MACRO)
        gaps = self._gaps(indices, sectors, holding_quotes, ranges, macro)
        duration = int((time.perf_counter() - started) * 1000)
        return base | {
            'indices': indices, 'sectors': sectors, 'holdings': holding_quotes,
            'macro': macro,
            'scopes': {'HOLDING': scope(holdings), 'WATCHING': scope(watching),
                       'MARKET': {'symbols': [], 'groups': group_headlines(market['items'])[:20],
                                  'unavailable_symbols': [] if market['status'] == 'AVAILABLE' else ['MARKET'],
                                  'not_entitled_symbols': ['MARKET'] if market['status'] == 'NOT_ENTITLED' else [],
                                  'failed_symbols': ([] if market['status'] in {'AVAILABLE', 'NOT_ENTITLED'}
                                                     else ['MARKET']),
                                  'coverage_status': ('AVAILABLE' if market['status'] == 'AVAILABLE' else
                                                      'REFUSED' if market['status'] == 'NOT_ENTITLED' else
                                                      'UNKNOWN'),
                                  'checked_count': 1, 'not_checked_symbols': [], 'screened': False,
                                  'meaning': ('General market headlines from the provider, grouped '
                                              'only to fold reposts. Not screened by a model.')}},
            'theme_scope': {
                'status': 'AVAILABLE' if watched_sectors else 'EMPTY',
                'watched_sectors': watched_sectors,
                'available_sectors': [code for code, _ in SECTORS],
                'basis': 'UNIVERSE_WATCHING_ENTRY_ON_A_SECTOR_ETF',
                'how_to_add': '在「研究范围」把行业 ETF（如 SMH）加入关注，即成为我的行业。',
                'meaning': ('A sector is "mine" only because you watch its ETF. Sector moves '
                            'are broad context for holdings, never a claim about one company.')},
            'gaps': gaps,
            'range_context': {
                'status': 'AVAILABLE' if self.range_source is not None else 'NOT_CONFIGURED',
                'provider': 'massive', 'price_basis': 'SPLIT_ADJUSTED',
                'sessions': RANGE_SESSIONS, 'symbol_limit': RANGE_SYMBOL_LIMIT,
                'symbols_requested': [s for s, _ in (BENCHMARKS + SECTORS)[:RANGE_SYMBOL_LIMIT]]
                if self.range_source is not None else [],
                'meaning': ('5/20-day change and 20-day realized volatility from completed daily '
                            'bars. Not a forecast, not a probability and not a trading signal. '
                            'Dividends are not adjusted, so the figures are price-only.')},
            'refresh_state': self._refresh_state(now, {'quotes': quotes, 'news': news,
                                                       'duration_ms': duration, 'forced': forced,
                                                       'requested': quote_symbols,
                                                       'range': ranges}),
            'budget': self._budget(quote_symbols, news_symbols),
        }

    def _gaps(self, indices, sectors, holdings, ranges, macro=()):
        """Every metric that could not be answered, with the reason it could not."""
        gaps = []
        for row in list(indices) + list(sectors) + list(macro) + list(holdings):
            if row.get('coverage_status') == 'AVAILABLE':
                continue
            gaps.append({'key': row['symbol'], 'label': row.get('label'), 'area': 'QUOTE',
                         'reason': row.get('status'),
                         'detail': row.get('detail') or self._gap_detail(row.get('status'))})
        for symbol, value in ranges.items():
            if value.get('status') in {'AVAILABLE', 'PARTIAL'}:
                continue
            gaps.append({'key': symbol, 'label': symbol, 'area': 'RANGE',
                         'reason': value.get('status'),
                         'detail': value.get('reason') or 'Completed daily bars unavailable'})
        return gaps

    @staticmethod
    def _gap_detail(status):
        return {'NOT_ENTITLED': '当前数据计划不包含该标的',
                'NO_PRICE': '数据源返回了响应但没有可用价格',
                'UNAVAILABLE': '数据源暂时不可用',
                'RATE_LIMITED': '触发数据源速率限制',
                'STALE': '报价超过四天未更新'}.get(status, '未知原因')

    def _budget(self, quote_symbols, news_symbols):
        return {'provider': 'finnhub', 'quote_calls_max': len(quote_symbols),
                'news_calls_max': len(news_symbols),
                'range_calls_max': RANGE_SYMBOL_LIMIT if self.range_source is not None else 0,
                'range_provider': 'massive' if self.range_source is not None else None,
                'model_calls': 0, 'min_forced_refresh_seconds': int(MIN_FORCED_REFRESH.total_seconds()),
                'rate_limit_assumption_per_minute': 60,
                'meaning': ('Worst-case request counts for one refresh. Data reads are free of '
                            'model cost; no headline is sent to a model from this endpoint.')}

    def _refresh_state(self, now, run):
        """What the last refresh actually achieved, kept across requests."""
        if run is not None:
            definite = [row for row in run['quotes'].values() if row.get('answer_kind') == 'DEFINITE']
            unknown = [row['symbol'] for row in run['quotes'].values()
                       if row.get('answer_kind') != 'DEFINITE']
            news_unknown = [key or 'MARKET' for key, value in run['news'].items()
                            if value['status'] not in {'AVAILABLE', 'NOT_ENTITLED'}]
            unknown += [symbol for symbol in news_unknown if symbol not in unknown]
            result = 'FAILED' if not definite else 'PARTIAL' if unknown else 'OK'
            self._refresh = {'attempts': self._refresh['attempts'] + 1,
                             'last_attempt_at': now.isoformat(),
                             'last_success_at': now.isoformat() if definite else self._refresh['last_success_at'],
                             'last_result': result, 'last_duration_ms': run['duration_ms'],
                             'last_uncovered': sorted(set(unknown)),
                             'last_forced': run['forced']}
        return {**self._refresh,
                'meaning': ('OK means every requested symbol got a definite answer, including a '
                            'refusal. PARTIAL and FAILED mean some symbols are simply unknown. '
                            'A definite refusal is still coverage: we know the plan excludes it.')}


def register_pulse(app, pulse):
    @app.get('/api/today/pulse')
    def today_pulse(refresh: int = 0):
        return pulse.snapshot(refresh=bool(refresh))
