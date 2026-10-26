"""Deterministic novelty, source-independence and scope projection for Today.

Reads only immutable stored receipts, aliases and scan identity. No model call,
no schema migration, no receipt mutation and no invented number. Every projection
carries the basis that produced it so the UI can say *why* a card is, or is not,
today's change.

Vocabulary is deliberately narrow. A class answers one question only:
"has the user already been able to see this?" It is never a materiality verdict,
a probability, a return forecast or a position size.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

from .attention_groups import publication_precision
from .domain import utc

Novelty = Literal['NEW_TO_US', 'MATERIAL_UPDATE', 'REPUBLISHED', 'AGED_IN', 'UNKNOWN']
Scope = Literal['HOLDING', 'WATCHING', 'CANDIDATE']

NOVELTY_CLASSES = ('NEW_TO_US', 'MATERIAL_UPDATE', 'REPUBLISHED', 'AGED_IN', 'UNKNOWN')
CURRENT_NOVELTY = frozenset({'NEW_TO_US', 'MATERIAL_UPDATE'})
EARLIER_NOVELTY = frozenset({'REPUBLISHED', 'AGED_IN', 'UNKNOWN'})
SCOPES = ('HOLDING', 'WATCHING', 'CANDIDATE')

# Same seven-day candidate window as attention.LOOKBACK. A compatible earlier
# event outside it is an old event arriving late, not a new development.
AGED_AFTER = timedelta(days=7)
RECENT_PRIOR_LIMIT = 40
AGED_PRIOR_LIMIT = 20
# Only two-level public suffixes that actually occur in US-equity news feeds.
# This is a suffix table, not a keyword dictionary.
MULTI_LABEL_SUFFIXES = frozenset({
    'co.uk', 'com.au', 'co.jp', 'co.nz', 'com.br', 'co.in', 'com.sg', 'co.za',
    'com.mx', 'com.hk', 'com.cn', 'co.kr', 'com.tw',
})
_STOP_TOKENS = frozenset(
    'the and with from after that this company news report reports stock stocks shares '
    'market says said will have has for its new update latest'.split()
)
_LEAD_PREFIX = re.compile(r'^(?:UPDATE\s*\d*|BREAKING|EXCLUSIVE|CORRECTION)\s*[-:—]\s*', re.I)


def _normalize(text: str | None) -> str:
    value = unicodedata.normalize('NFKC', text or '')
    value = _LEAD_PREFIX.sub('', value)
    return re.sub(r'\s+', ' ', value).strip().casefold()


def registrable_domain(uri: str | None) -> str | None:
    """Registrable domain for source-independence counting, never a trust score."""
    if not uri:
        return None
    try:
        host = urlsplit(uri).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().strip('.')
    labels = [part for part in host.split('.') if part]
    if len(labels) < 2:
        return host or None
    if '.'.join(labels[-2:]) in MULTI_LABEL_SUFFIXES and len(labels) >= 3:
        return '.'.join(labels[-3:])
    return '.'.join(labels[-2:])


def prose_identity(receipt) -> str | None:
    """Exact prose identity of one preserved receipt.

    Equality means the same words were already stored; it does not prove the same
    underlying event. Different outlets that rewrite the lede deliberately differ.
    """
    title, excerpt = _normalize(receipt.title), _normalize(receipt.excerpt)
    if not title and not excerpt:
        return None
    digest = hashlib.sha256((title + '|' + excerpt).encode('utf-8')).hexdigest()
    return 'prose:' + digest


def _tokens(text: str | None) -> set[str]:
    return {token for token in re.findall(r'[a-z][a-z0-9]{2,}', _normalize(text))
            if token not in _STOP_TOKENS}


def _numbers(text: str | None) -> tuple[str, ...]:
    return tuple(sorted(set(re.findall(r'\d+(?:[.,]\d+)*%?', _normalize(text)))))


def _likely_same_origin(left, right) -> bool:
    """Cheap wire-copy heuristic: same numbers and a near-identical headline.

    Deliberately *not* used to raise the independent-source count or to merge
    receipts. It only labels a count as uncertain.
    """
    if _numbers((left.title or '') + ' ' + (left.excerpt or '')) != \
            _numbers((right.title or '') + ' ' + (right.excerpt or '')):
        return False
    a, b = _tokens(left.title), _tokens(right.title)
    if len(a) < 4 or len(b) < 4:
        return False
    return len(a & b) / len(a | b) >= 0.85


def _cluster_count(receipts, same) -> int:
    parent = list(range(len(receipts)))

    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i in range(len(receipts)):
        for j in range(i + 1, len(receipts)):
            if same(receipts[i], receipts[j]):
                parent[root(j)] = root(i)
    return len({root(index) for index in range(len(receipts))})


def publisher_identity(receipt) -> str | None:
    """Who published this, as a stable identity.

    Real feeds return an aggregator URL (every Finnhub receipt points at
    finnhub.io) but carry the actual publisher in `source`. Counting URL domains
    against that feed would report one source for every story, so the publisher
    name wins when present and the domain is only a fallback.
    """
    data = receipt.structured_data or {}
    for key in ('source', 'publisher', 'outlet'):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return 'publisher:' + re.sub(r'\s+', ' ', value.strip()).casefold()
    domain = registrable_domain(receipt.source_uri)
    return 'domain:' + domain if domain else None


def _identity_label(identity: str) -> str:
    return identity.split(':', 1)[1]


def source_projection(receipts) -> dict:
    """Independent origins among the receipts this unit actually preserved.

    A wire story republished verbatim by three publishers is one origin. Two
    distinct articles from one publisher are one outlet. Rewritten copies are
    counted separately but labelled uncertain, because similarity is not proof
    of origin. Neither count is evidence of truth.
    """
    receipts = list(receipts)
    origins: dict[str, dict] = {}
    identities = []
    for receipt in receipts:
        identity = publisher_identity(receipt)
        identities.append(identity)
        key = prose_identity(receipt) or 'receipt:' + receipt.evidence_id
        origin = origins.setdefault(key, {'outlets': set(), 'receipts': []})
        origin['receipts'].append(receipt.evidence_id)
        if identity:
            origin['outlets'].add(identity)
    outlets: set[str] = set()
    publishers: set[str] = set()
    unresolved = 0
    syndicated = False
    for origin in origins.values():
        if len(origin['outlets']) > 1:
            syndicated = True
        publishers |= origin['outlets']
        if origin['outlets']:
            # One origin counts once. Every publisher that carried it stays visible,
            # because "3 篇原文 · 1 个来源" is the useful sentence, not "Benzinga".
            outlets.add(sorted(origin['outlets'])[0])
        else:
            # No publisher and no usable URI: the origin cannot be counted.
            unresolved += 1
    count = len(outlets)
    near_origins = _cluster_count(receipts, _likely_same_origin) if len(receipts) > 1 else count
    if not receipts:
        status = 'UNKNOWN'
    elif syndicated and count <= 1:
        status = 'SYNDICATED'
    elif near_origins < count:
        status = 'LIKELY_SYNDICATED'
    elif count >= 2:
        status = 'MULTI_SOURCE'
    elif count == 1:
        status = 'SINGLE_SOURCE'
    else:
        status = 'UNKNOWN'
    bases = {identity.split(':', 1)[0] for identity in identities if identity}
    return {
        'receipt_count': len(receipts),
        'distinct_prose': len(origins),
        'independent_sources': count,
        'unresolved_receipts': unresolved,
        'near_duplicate_origins': near_origins,
        'syndicated': syndicated,
        'status': status,
        'outlets': sorted(_identity_label(identity) for identity in outlets)[:8],
        'publishers': sorted(_identity_label(identity) for identity in publishers)[:8],
        'publisher_count': len(publishers),
        'identity_basis': ('MIXED' if len(bases) > 1 else
                           bases.pop().upper() if bases else 'NONE'),
        'meaning': ('Independent origins among preserved receipts; never a truth, '
                    'importance or confidence score. Publisher identity is used when the '
                    'feed supplies it, because aggregator URLs share one domain. '
                    'One story carried by several publishers is one origin and is '
                    'reported as syndicated, not as independent confirmation.'),
    }


def _plain(value):
    if value is None:
        return None
    if isinstance(value, (str, int, float)):
        return value
    return str(value)


def scope_projection(universe) -> dict:
    """Scope comes from the portfolio-derived universe entry, never from news text."""
    data = universe if isinstance(universe, dict) else universe.model_dump(mode='json')
    state = data.get('state')
    scope = state if state in SCOPES else 'CANDIDATE'
    exposure = _plain(data.get('cost_exposure_pct'))
    basis = data.get('exposure_basis') or 'UNKNOWN'
    return {
        'scope': scope,
        'exposure_pct': exposure,
        'exposure_basis': basis if exposure is not None else 'UNKNOWN',
        'meaning': ('Cost exposure to cash, not current market weight.' if exposure is not None
                    else 'No qualified exposure; missing marks stay unknown, never zero.'),
    }


def _event_date(receipt) -> datetime | None:
    raw = (receipt.structured_data or {}).get('event_date')
    if not isinstance(raw, str) or not raw.strip():
        return None
    for candidate in (raw.strip(), raw.strip() + 'T00:00:00+00:00'):
        try:
            value = datetime.fromisoformat(candidate.replace('Z', '+00:00'))
        except ValueError:
            continue
        # A date-only value has no zone; UTC is the storage convention, not a
        # claim about the event's local time.
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def lineage_receipts(store, research, event_keys) -> list:
    """Receipts that were folded into an event, read for source counting only.

    Folding three publishers into one event is right; throwing away who published
    them is not, because the card then reports one source for a story the market
    carried three times. These receipts are never screened, ordered, cited as new
    evidence or used for novelty: they exist so the count can be honest.
    """
    from .domain import EvidenceItem
    keys = [key for key in dict.fromkeys(event_keys or ()) if key]
    if not keys:
        return []
    placeholders = ','.join('?' * len(keys))
    with store._connect() as db:
        rows = db.execute(
            'SELECT payload,payload_hash FROM attention_event_receipts '
            f'WHERE event_key IN ({placeholders}) ORDER BY evidence_id', keys).fetchall()
    receipts = []
    for payload, digest in rows:
        try:
            receipts.append(EvidenceItem.model_validate(research.checked(payload, digest)))
        except Exception:
            # An unreadable folded receipt must not fail the queue; the card simply
            # reports the receipts it can prove.
            continue
    return receipts


def _prior_events(store, research, symbol: str, exclude: str, now: datetime):
    """Bounded read of earlier stored packets for one symbol. Read-only.

    Two windows keep this cheap on a polling endpoint: the most recent events
    (for republish/revision detection) and the most recent events that already
    fell outside the candidate window (for aged-in detection).
    """
    from .attention import EventPacket
    cutoff = (now - AGED_AFTER).isoformat()
    with store._connect() as db:
        rows = db.execute(
            'SELECT e.event_key,e.payload,e.payload_hash,s.started_at '
            'FROM attention_events e JOIN attention_scans s ON s.scan_id=e.scan_id '
            'WHERE e.symbol=? AND e.event_key<>? AND s.started_at<=? '
            'ORDER BY s.started_at DESC,e.rowid DESC LIMIT ?',
            (symbol, exclude, now.isoformat(), RECENT_PRIOR_LIMIT)).fetchall()
        aged = db.execute(
            'SELECT e.event_key,e.payload,e.payload_hash,s.started_at '
            'FROM attention_events e JOIN attention_scans s ON s.scan_id=e.scan_id '
            'WHERE e.symbol=? AND e.event_key<>? AND s.started_at<? '
            'ORDER BY s.started_at DESC,e.rowid DESC LIMIT ?',
            (symbol, exclude, cutoff, AGED_PRIOR_LIMIT)).fetchall()
    result, seen = [], set()
    for key, payload, digest, started in rows + aged:
        if key in seen:
            continue
        seen.add(key)
        try:
            # `started_at` comes back as text; the sibling rule compares instants.
            recorded = utc(datetime.fromisoformat(started)) if started else None
            result.append((key, EventPacket.model_validate(research.checked(payload, digest)), recorded))
        except Exception:
            # An unreadable historical payload must not fail the whole queue.
            continue
    return result


def _confidence(primary) -> str:
    """How strongly the identity evidence supports NEW_TO_US, never investment confidence."""
    title = _normalize(primary.title)
    tokens = {t for t in re.findall(r'[a-z][a-z0-9]{2,}', title) if t not in _STOP_TOKENS}
    han = len(re.findall(r'[\u4e00-\u9fff]', title))
    if publication_precision(primary) == 'INSTANT' and len(tokens) >= 6:
        return 'HIGH'
    # A Chinese headline has no Latin tokens; length is the only available proxy.
    if len(tokens) >= 3 or han >= 12:
        return 'MEDIUM'
    return 'LOW'


def _same_event_content(left, right) -> bool:
    """Content match without a time window, for old events arriving late.

    Reuses the conservative signature from the repost projection (same numbers,
    named subjects, disclosure stages, symbol and thesis) but deliberately drops
    the 36-hour proximity rule, because "old news republished today" is by
    definition far from the original. A false positive only moves a card to the
    earlier section; it never deletes evidence or changes an Action.
    """
    if not left or not right:
        return False
    for field in ('numbers', 'stages', 'source_event_identity', 'symbol', 'thesis'):
        if left[field] != right[field]:
            return False
    # A headline's first word is always capitalised, so exact name-set equality
    # would reject a simple reordering ("Safety inspection at Aurora Grid ...").
    # Require containment instead; numbers and disclosure stages stay exact.
    left_names, right_names = left['names'], right['names']
    if left_names or right_names:
        smaller = min(len(left_names), len(right_names))
        if not smaller or len(left_names & right_names) / smaller < 0.8:
            return False
    common = len(left['tokens'] & right['tokens'])
    union = len(left['tokens'] | right['tokens'])
    if not union:
        return False
    return common / union >= 0.72 or common / min(len(left['tokens']), len(right['tokens'])) >= 0.85


def novelty_projection(store, research, item, *, now: datetime, priors=None,
                       first_seen_at: datetime | None = None) -> dict:
    """One class plus its basis for one AttentionItem. Never mutates anything.

    `first_seen_at` is when the system first recorded this event. It is required to
    break the sibling case: two identical-prose events from different URLs would
    otherwise each treat the other as earlier coverage and both leave Today.
    """
    receipts = list(item.packet.evidence)
    primary = receipts[0]
    base = {'classes': list(NOVELTY_CLASSES), 'prior_event_key': None, 'prior_published_at': None}
    if not primary.title or publication_precision(primary) == 'UNKNOWN':
        return {**base, 'novelty': 'UNKNOWN', 'confidence': 'LOW',
                'basis': 'UNQUALIFIED_PUBLICATION_TIME',
                'explanation': '发布时间精度未知，不能判定新旧。'}
    # A revision packet legitimately carries the earlier receipt; only the new
    # receipt can be evidence that *this* delivery is a republish.
    new_receipts = receipts[:1] if item.packet.revision_of else receipts
    prose = {value for value in (prose_identity(r) for r in new_receipts) if value}
    priors = _prior_events(store, research, item.symbol, item.item_id, now) if priors is None else priors
    started = first_seen_at

    def not_earlier(prior_started) -> bool:
        # A prior recorded at or after this event is a sibling, not earlier coverage.
        return bool(started and prior_started and prior_started >= started)

    # 1. Ingest already decided this is the same article. A new timestamp with
    # unchanged words is a re-delivery, not a new development.
    if item.packet.revision_of:
        unchanged = (len(receipts) > 1 and
                     prose_identity(receipts[0]) == prose_identity(receipts[1]))
        if unchanged:
            earlier = receipts[1].published_at
            return {**base, 'novelty': 'REPUBLISHED', 'confidence': 'HIGH',
                    'basis': 'TIMESTAMP_ONLY_REDELIVERY', 'prior_event_key': item.packet.revision_of,
                    'prior_published_at': earlier.isoformat() if earlier else None,
                    'explanation': '文字未变，仅发布时间更新；不是新的经营发展。'}
        return {**base, 'novelty': 'MATERIAL_UPDATE', 'confidence': 'HIGH',
                'basis': 'REVISION_OF_STORED_ARTICLE', 'prior_event_key': item.packet.revision_of,
                'explanation': '同一原文的事实或披露阶段发生变化，属于事件更新。'}

    # 2. The exact words are already stored. A new timestamp cannot make them new.
    for key, packet, prior_started in priors:
        if key == item.item_id:
            continue  # A cached prior list may contain this very event.
        if packet.revision_of == item.item_id:
            continue  # A later revision of this item is not an earlier original.
        if not_earlier(prior_started):
            continue  # Same-scan or later sibling; ingest already kept both articles.
        stored = {value for value in (prose_identity(r) for r in packet.evidence) if value}
        if prose & stored:
            earlier = packet.evidence[0].published_at
            return {**base, 'novelty': 'REPUBLISHED', 'confidence': 'HIGH',
                    'basis': 'PROSE_ALREADY_STORED', 'prior_event_key': key,
                    'prior_published_at': earlier.isoformat() if earlier else None,
                    'explanation': '同一原文已保存；本次仅为转载或重新送达。'}

    # 3. A declared event date outside the candidate window is old news by construction.
    declared = _event_date(primary)
    if declared is not None and declared < now - AGED_AFTER:
        return {**base, 'novelty': 'AGED_IN', 'confidence': 'HIGH',
                'basis': 'EVENT_DATE_PRECEDES_WINDOW', 'prior_published_at': declared.isoformat(),
                'explanation': '来源声明的事件日期早于候选窗口；发布时间新不代表事件新。'}

    # 4. Same event content, no time window: an old event arriving late.
    from .attention_preselection import signature
    current = signature(item.packet)
    if current:
        for key, packet, prior_started in priors:
            if key == item.item_id or packet.revision_of == item.item_id:
                continue
            if not_earlier(prior_started):
                continue
            if not _same_event_content(current, signature(packet)):
                continue
            earlier = packet.evidence[0].published_at
            if earlier and earlier < now - AGED_AFTER:
                return {**base, 'novelty': 'AGED_IN', 'confidence': 'MEDIUM',
                        'basis': 'COMPATIBLE_EVENT_OUTSIDE_WINDOW', 'prior_event_key': key,
                        'prior_published_at': earlier.isoformat(),
                        'explanation': '与已保存事件同主体、同数字、同阶段，但原事件已超出窗口。'}
            return {**base, 'novelty': 'REPUBLISHED', 'confidence': 'MEDIUM',
                    'basis': 'NEAR_DUPLICATE_OF_STORED_EVENT', 'prior_event_key': key,
                    'prior_published_at': earlier.isoformat() if earlier else None,
                    'explanation': '与已保存事件几乎一致，未见新的数字或阶段。'}

    return {**base, 'novelty': 'NEW_TO_US', 'confidence': _confidence(primary),
            'basis': 'NO_PRIOR_MATCH',
            'explanation': '未找到同一原文或同事件的历史记录；这是本轮首次进入系统。'}


CURRENT_MAX_PUBLISHED_AGE_HOURS = 48
CURRENT_MAX_FIRST_SEEN_AGE_HOURS = 24


def placement(novelty: str, freshness: dict | None, *, stale_reason: str | None = None,
              times: dict | None = None) -> str:
    """CURRENT only when the change is new to us, recent, and the scan is not stale.

    Two clocks, because "recent" has two meanings: an article published within two
    days, or an article we only just discovered even if it is older. Without this,
    an unresolved card from six days ago still counts as today's change and the
    page silently becomes a backlog console.
    """
    if stale_reason:
        return 'EARLIER'
    if novelty not in CURRENT_NOVELTY:
        return 'EARLIER'
    status = (freshness or {}).get('freshness_status')
    if status not in {'TODAY', 'RECENT'}:
        return 'EARLIER'
    if times is None:
        return 'CURRENT'
    published = times.get('published_age_hours')
    seen = times.get('first_seen_age_hours')
    if published is not None and published <= CURRENT_MAX_PUBLISHED_AGE_HOURS:
        return 'CURRENT'
    if seen is not None and seen <= CURRENT_MAX_FIRST_SEEN_AGE_HOURS:
        return 'CURRENT'
    return 'EARLIER'


# ---------------------------------------------------------------- event class
# A deliberately small taxonomy. SEC form/item codes come first because they are
# legally defined; the news patterns are one hint per class, used for ordering and
# labelling only. A match never dismisses a card and never overrides the model's
# materiality judgement or the deterministic Validator.
EVENT_CLASSES = ('GUIDANCE', 'REGULATORY', 'CUSTOMER_CONTRACT', 'OPERATIONS', 'CAPITAL',
                 'M_AND_A', 'RESULTS', 'PRODUCT', 'GOVERNANCE', 'OTHER')

EVENT_CLASS_LABELS = {
    'GUIDANCE': '指引/展望变化', 'REGULATORY': '监管或法律', 'CUSTOMER_CONTRACT': '客户与合同',
    'OPERATIONS': '经营中断或资产问题', 'CAPITAL': '资本与摊薄', 'M_AND_A': '并购与交易',
    'RESULTS': '业绩披露', 'PRODUCT': '产品与审批', 'GOVERNANCE': '治理与人事', 'OTHER': '其他',
}

# Relative decision value for a holding. Not a probability, not a return.
EVENT_CLASS_WEIGHT = {
    'GUIDANCE': 25, 'REGULATORY': 24, 'CUSTOMER_CONTRACT': 22, 'OPERATIONS': 20,
    'M_AND_A': 20, 'CAPITAL': 18, 'RESULTS': 16, 'PRODUCT': 12, 'GOVERNANCE': 10, 'OTHER': 4,
}

SEC_ITEM_CLASS = {
    '1.01': 'CUSTOMER_CONTRACT', '1.02': 'CUSTOMER_CONTRACT', '1.03': 'OPERATIONS',
    '1.05': 'OPERATIONS', '2.01': 'M_AND_A', '2.02': 'RESULTS', '2.03': 'OPERATIONS',
    '2.04': 'OPERATIONS', '2.05': 'OPERATIONS', '2.06': 'OPERATIONS', '3.01': 'GOVERNANCE',
    '4.02': 'GOVERNANCE', '5.01': 'GOVERNANCE', '5.02': 'GOVERNANCE',
}
SEC_FORM_CLASS = {'10-Q': 'RESULTS', '10-K': 'RESULTS', 'S-1': 'CAPITAL', 'S-3': 'CAPITAL',
                  '424B5': 'CAPITAL', 'DEF 14A': 'GOVERNANCE', 'SC 13D': 'GOVERNANCE'}

# One pattern per class, ordered from most to least decision-relevant.
NEWS_TRIGGERS = (
    ('GUIDANCE', r'\bguidance\b|\boutlook\b|\bforecast\b|'
                 r'\b(?:raises?|lifts?|cuts?|lowers?)\b.{0,30}\b(?:full[- ]?year|annual|outlook|guidance)\b|'
                 r'\bpreliminary (?:results|revenue)\b'),
    ('REGULATORY', r'\b(?:sec|doj|ftc|fda|ema|regulator\w*|subpoena|investigat\w+|probe|'
                   r'consent order|warning letter|complete response letter|delist\w*|'
                   r'trading halt|class action)\b'),
    ('CUSTOMER_CONTRACT', r'\b(?:contract|agreement|customer|purchase order|supply|partnership)\b'
                          r'.{0,60}\b(?:terminat\w+|cancel\w+|lost|loss|suspend\w+|wins?|won|signed|awarded|renew\w+)\b'),
    ('CAPITAL', r'\b(?:offering|dilut\w+|convertible|private placement|shelf|buyback|'
                r'repurchase|dividend|credit facility|going concern|bankrupt\w*)\b'),
    ('M_AND_A', r'\b(?:acqui\w+|merger|takeover|tender offer|spin[- ]?off|stake sale)\b'),
    ('OPERATIONS', r'\b(?:halt\w*|recall|outage|explosion|fire|strike|shutdown|restatement|'
                   r'impairment|writedown|write-down)\b'),
    ('PRODUCT', r'\b(?:launch\w*|approval|clearance|patent|trial results?|phase [123])\b'),
    ('GOVERNANCE', r'\b(?:resign\w*|appoint\w*|chief executive|chief financial|board|auditor)\b'),
    ('RESULTS', r'\b(?:quarterly|first quarter|second quarter|third quarter|fourth quarter|'
                r'fiscal year|earnings|revenue of|net loss|net income)\b'),
)
_NEWS_PATTERNS = tuple((name, re.compile(pattern, re.I)) for name, pattern in NEWS_TRIGGERS)


def event_class_projection(item) -> dict:
    """Typed event class from structured disclosure metadata first, then one news hint."""
    for receipt in item.packet.evidence:
        data = receipt.structured_data or {}
        if receipt.evidence_type == 'sec_filing':
            for code in (data.get('items') or []):
                mapped = SEC_ITEM_CLASS.get(str(code).strip())
                if mapped:
                    return {'event_class': mapped, 'label': EVENT_CLASS_LABELS[mapped],
                            'basis': f'SEC_ITEM_{code}', 'weight': EVENT_CLASS_WEIGHT[mapped]}
            form = str(data.get('form') or '').strip().upper()
            if form in SEC_FORM_CLASS:
                mapped = SEC_FORM_CLASS[form]
                return {'event_class': mapped, 'label': EVENT_CLASS_LABELS[mapped],
                        'basis': f'SEC_FORM_{form}', 'weight': EVENT_CLASS_WEIGHT[mapped]}
    text = ' '.join((receipt.title or '') + ' ' + (receipt.excerpt or '')
                    for receipt in item.packet.evidence[:2])
    for name, pattern in _NEWS_PATTERNS:
        if pattern.search(text):
            return {'event_class': name, 'label': EVENT_CLASS_LABELS[name],
                    'basis': 'NEWS_TRIGGER', 'weight': EVENT_CLASS_WEIGHT[name]}
    return {'event_class': 'OTHER', 'label': EVENT_CLASS_LABELS['OTHER'],
            'basis': 'NO_TRIGGER', 'weight': EVENT_CLASS_WEIGHT['OTHER']}


# ---------------------------------------------------------------- importance
SCOPE_WEIGHT = {'HOLDING': 40, 'WATCHING': 20, 'CANDIDATE': 6}
NOVELTY_WEIGHT = {'NEW_TO_US': 15, 'MATERIAL_UPDATE': 12}
MATERIALITY_WEIGHT = {'HIGH': 15, 'MEDIUM': 8, 'UNKNOWN': 3, 'LOW': 0, 'NONE': 0}
URGENCY_WEIGHT = {'NOW': 8, 'SOON': 4, 'UNKNOWN': 2, 'ROUTINE': 0}
BAND_CRITICAL = 75
BAND_IMPORTANT = 50


def importance_projection(item, radar: dict) -> dict:
    """Deterministic ordering score. Never a probability, return or position size."""
    event_class = radar['event_class']
    scope = radar['scope']['scope']
    sources = radar['sources']
    screen = item.screen
    score = (SCOPE_WEIGHT.get(scope, 0) + event_class['weight']
             + NOVELTY_WEIGHT.get(radar['novelty'], 0)
             + min(sources['independent_sources'], 3) * 3
             + URGENCY_WEIGHT.get(screen.urgency, 0)
             + MATERIALITY_WEIGHT.get(screen.materiality, 0))
    band = ('CRITICAL' if score >= BAND_CRITICAL else
            'IMPORTANT' if score >= BAND_IMPORTANT else 'ROUTINE')
    reasons = [f'范围 {scope}', f'类型 {event_class["label"]}',
               f'新旧 {radar["novelty"]}', f'独立来源 {sources["independent_sources"]}']
    if screen.materiality in {'HIGH', 'MEDIUM'}:
        reasons.append(f'模型重大性 {screen.materiality}')
    return {'score': score, 'band': band, 'reasons': reasons,
            'meaning': 'Ordering only; not a probability, return or trade size.'}


def time_semantics(item, *, first_seen_at, now: datetime) -> dict:
    """Four distinct times. A publication date is not an event time."""
    primary = item.packet.evidence[0]
    declared = _event_date(primary)
    published = primary.published_at
    return {
        'published_at': published.isoformat() if published else None,
        'retrieved_at': primary.retrieved_at.isoformat() if primary.retrieved_at else None,
        'first_seen_at': first_seen_at.isoformat() if first_seen_at else None,
        'event_time': declared.isoformat() if declared else None,
        'event_time_verified': declared is not None,
        'publication_precision': publication_precision(primary),
        'published_age_hours': (round((now - published).total_seconds() / 3600.0, 2)
                                if published else None),
        'first_seen_age_hours': (round((now - first_seen_at).total_seconds() / 3600.0, 2)
                                 if first_seen_at else None),
        'basis': 'SOURCE_DECLARED_EVENT_TIME' if declared else 'PUBLICATION_ONLY',
        'meaning': ('首次看到时间来自创建该事件的扫描；发布时间不等于事件发生时间。'
                    if first_seen_at else '首次看到时间未记录。'),
    }


def _first_seen(store, item) -> datetime | None:
    with store._connect() as db:
        row = db.execute('SELECT started_at FROM attention_scans WHERE scan_id=?',
                         (item.scan_id,)).fetchone()
    return utc(datetime.fromisoformat(row[0])) if row and row[0] else None


def project_row(store, research, row, *, now: datetime, priors=None,
                stale_reason: str | None = None) -> dict:
    """Attach the radar projection to one queue row without touching its payload."""
    from .attention import AttentionItem
    from .domain import EvidenceItem
    item = AttentionItem.model_validate(row['item'])
    # Reposts folded into this unit are the event itself, not earlier coverage of it:
    # comparing against them labelled a brand-new APP event REPUBLISHED (CONVERGENCE-001).
    members = set(row.get('research_event', {}).get('member_item_ids') or ())
    folded = {receipt.evidence_id for receipt in item.packet.evidence}
    if priors is not None:
        priors = [prior for prior in priors if prior[0] not in members
                  and not {r.evidence_id for r in prior[1].evidence} <= folded]
    first_seen = _first_seen(store, item)
    novelty = novelty_projection(store, research, item, now=now, priors=priors,
                                 first_seen_at=first_seen)
    projected = row.get('research_event', {}).get('evidence')
    receipts = ([EvidenceItem.model_validate(receipt) for receipt in projected]
                if projected else list(item.packet.evidence))
    # Folded receipts are not new evidence, but they are provable provenance.
    lineage_keys = [item.packet.event_key]
    lineage_keys += list((((row.get('item') or {}).get('screening_lineage') or {})
                          .get('candidate_repost_unit') or {}).get('member_event_keys') or ())
    kept = {receipt.evidence_id for receipt in receipts}
    receipts += [receipt for receipt in lineage_receipts(store, research, lineage_keys)
                 if receipt.evidence_id not in kept]
    sources = source_projection(receipts)
    scope = scope_projection(row.get('current_universe') or item.universe)
    radar = {**novelty, 'sources': sources, 'scope': scope}
    radar['event_class'] = event_class_projection(item)
    radar['importance'] = importance_projection(item, radar)
    radar['times'] = time_semantics(item, first_seen_at=first_seen, now=now)
    radar['placement'] = placement(novelty['novelty'], row.get('freshness'),
                                   stale_reason=stale_reason, times=radar['times'])
    if stale_reason:
        radar['placement_reason'] = stale_reason
    return {**row, 'radar': radar}


def project_rows(store, research, rows, *, now: datetime,
                 stale_reason: str | None = None) -> list[dict]:
    cache: dict[str, list] = {}
    output = []
    for row in rows:
        symbol = row['item']['symbol']
        if symbol not in cache:
            cache[symbol] = _prior_events(store, research, symbol, '', now)
        output.append(project_row(store, research, row, now=now, priors=cache[symbol],
                                  stale_reason=stale_reason))
    return output


def order_by_importance(rows) -> list[dict]:
    """Holdings first by decision value, then newest. Stable and inspectable."""
    return sorted(rows, key=lambda row: (
        row['radar']['importance']['score'],
        row['radar']['importance']['band'] == 'CRITICAL',
        row['item']['event_time'],
        row['item']['item_id']), reverse=True)


SECTION_TITLES = {'HOLDING': '持仓变化', 'WATCHING': '关注变化', 'CANDIDATE': '其他研究范围'}
SECTION_NOTES = {
    'HOLDING': '来自组合持仓；敞口按成本与现金口径，缺价时保持未知。',
    'WATCHING': '你主动跟踪的标的，不是持仓。',
    'CANDIDATE': '待评估范围；优先级低于持仓与关注。',
}
SECTION_EMPTY = {
    'HOLDING': '本次没有持仓变化。',
    'WATCHING': '本次没有关注标的变化。',
    'CANDIDATE': '本次没有其他研究范围的变化。',
}
SECTION_EMPTY_STALE = '数据截至 {when}，没有覆盖今天；这里为空不代表没有变化。'


def build_sections(rows, *, coverage: dict | None = None) -> list[dict]:
    """Always three scope sections. An empty section is a statement, not an absence.

    The Owner could not tell whether holdings were classified or simply missing, so
    omission is no longer allowed: each scope reports its own count and, when empty,
    says so explicitly with the data cutoff.
    """
    when = (coverage or {}).get('last_successful_scan_at')
    state = (coverage or {}).get('state')
    stale = state in {'STALE', 'OLD', 'NEVER'}
    sections = []
    for scope in SCOPES:
        members = [row for row in rows if row['radar']['scope']['scope'] == scope]
        empty = SECTION_EMPTY[scope]
        if stale and when:
            empty = empty + ' ' + SECTION_EMPTY_STALE.format(when=when[:16].replace('T', ' '))
        sections.append({
            'key': scope,
            'title': SECTION_TITLES[scope],
            'note': SECTION_NOTES[scope],
            'count': len(members),
            'empty_sentence': None if members else empty,
            'items': members,
        })
    return sections


def headline(rows, *, coverage: dict | None = None) -> dict | None:
    """One deterministic sentence answering "what matters most today"."""
    if not rows:
        state = (coverage or {}).get('state')
        when = (coverage or {}).get('last_successful_scan_at')
        if state in {'STALE', 'OLD', 'NEVER'}:
            return {'available': False, 'state': state,
                    'sentence': f'今天没有可用的覆盖数据（最近成功扫描 {when or "无"}）。',
                    'meaning': 'No card may be presented as today\'s change without a fresh scan.'}
        return {'available': False, 'state': state or 'FRESH',
                'sentence': '今天没有需要优先处理的持仓或关注变化。',
                'meaning': 'Empty is a valid result; it is not a failure to report.'}
    top = rows[0]
    radar = top['radar']
    brief = top['item']['screen'].get('brief') if isinstance(top['item']['screen'], dict) else None
    title = ((brief or {}).get('headline') or {}).get('text') or top['item'].get('what_changed', {}).get('text')
    sources = radar['sources']
    source_text = (f'{sources["independent_sources"]} 个独立来源'
                   if sources['independent_sources'] else '来源未定')
    return {
        'available': True,
        'symbol': top['item']['symbol'],
        'scope': radar['scope']['scope'],
        'event_class': radar['event_class']['event_class'],
        'event_class_label': radar['event_class']['label'],
        'band': radar['importance']['band'],
        'score': radar['importance']['score'],
        'novelty': radar['novelty'],
        'sources': source_text,
        'title': title,
        'sentence': (f'今天最值得先看：{top["item"]["symbol"]} · {radar["event_class"]["label"]}'
                     f' · {radar["novelty"]} · {source_text}'),
        'meaning': 'Deterministic ordering over the shown cards; not investment advice.',
    }


def summarize(rows, earlier_rows) -> dict:
    novelty = {key: 0 for key in NOVELTY_CLASSES}
    scopes = {key: 0 for key in SCOPES}
    source_status = {'SINGLE_SOURCE': 0, 'MULTI_SOURCE': 0, 'SYNDICATED': 0,
                     'LIKELY_SYNDICATED': 0, 'UNKNOWN': 0}
    bands = {'CRITICAL': 0, 'IMPORTANT': 0, 'ROUTINE': 0}
    classes = {key: 0 for key in EVENT_CLASSES}
    for row in list(rows) + list(earlier_rows):
        radar = row['radar']
        novelty[radar['novelty']] = novelty.get(radar['novelty'], 0) + 1
        scopes[radar['scope']['scope']] = scopes.get(radar['scope']['scope'], 0) + 1
        source_status[radar['sources']['status']] = source_status.get(radar['sources']['status'], 0) + 1
        if 'importance' in radar:
            bands[radar['importance']['band']] = bands.get(radar['importance']['band'], 0) + 1
            classes[radar['event_class']['event_class']] = classes.get(
                radar['event_class']['event_class'], 0) + 1
    receipts = sum(row['radar']['sources']['receipt_count'] for row in list(rows) + list(earlier_rows))
    independent = sum(row['radar']['sources']['independent_sources'] for row in list(rows) + list(earlier_rows))
    return {
        'version': 'today-radar-v2',
        'current_shown': len(rows),
        'earlier_shown': len(earlier_rows),
        'novelty': novelty,
        'scope': scopes,
        'source_status': source_status,
        'bands': bands,
        'event_classes': classes,
        'receipts_shown': receipts,
        'independent_sources_shown': independent,
        'redundant_receipts_avoided': max(0, receipts - independent),
        'meaning': ('Counts describe the cards shown, not the whole universe. '
                    'Novelty is a storage fact, not materiality.'),
    }
