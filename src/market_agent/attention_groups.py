"""Conservative, auditable review units over immutable Attention receipts.

The existing screening model proposes semantic equivalence. Python checks
membership and protected boundaries; it does not claim to prove equivalence.
"""
import json
import re
from datetime import timedelta
from typing import Literal

from .domain import FrozenModel
from .llm import LLMError
from .tdb import TDBError, content_hash


class DeepApplicability(FrozenModel):
    status: Literal['CURRENT', 'INPUT_CHANGED', 'NOT_IN_CURRENT_QUEUE', 'UNKNOWN']
    frozen_input_hash: str | None = None
    current_input_hash: str | None = None


def project_deep(research, deep, row):
    """Read-only validity against this queue observation, never mutate history.

    The checkpoint supplies RUNNING input identity. Legacy terminal payloads
    supply their original input_hash without synthesizing/upgrading a checkpoint.
    No active row means historical readback, not proof of current applicability.
    """
    if not deep:
        return None
    with research.store._connect() as db:
        checkpoint = db.execute('SELECT payload,payload_hash FROM attention_deep_inputs WHERE deep_id=?',
                                (deep['deep_id'],)).fetchone()
    result_hash = (deep['result'] or {}).get('input_hash')
    frozen_hash = content_hash(research.checked(*checkpoint)) if checkpoint else result_hash
    if checkpoint and result_hash and result_hash != frozen_hash:
        raise TDBError('ATTENTION_HISTORY_INVALID', 409)
    current_hash = content_hash(freeze_row(research, row).model_dump(mode='json')) if row else None
    validity = DeepApplicability(status='NOT_IN_CURRENT_QUEUE' if not row else
        'UNKNOWN' if not frozen_hash else 'CURRENT' if frozen_hash == current_hash else 'INPUT_CHANGED',
        frozen_input_hash=frozen_hash, current_input_hash=current_hash)
    return {**deep, 'input_changed': validity.status == 'INPUT_CHANGED',
            'applicability': validity.model_dump(mode='json')}


from .market_evidence import INSTANT_PUBLICATION_PROVIDERS  # noqa: E402  shared with market_identity


def publication_precision(receipt):
    value = (receipt.structured_data or {}).get('published_at_precision')
    # Legacy Finnhub/synthetic receipts have exact instants. Other unspecified
    # sources are unknown: never infer precision from midnight or retrieval time.
    return value if value in {'INSTANT', 'DATE', 'UNKNOWN'} else (
        'INSTANT' if receipt.published_at and receipt.provider in INSTANT_PUBLICATION_PROVIDERS else 'UNKNOWN')


def compatible_members(packets, screens):
    from .attention import eligible
    times = [p.evidence[0].published_at for p in packets]
    return (len({p.universe.symbol for p in packets}) == 1 and
        not any(p.revision_of for p in packets) and
        all(publication_precision(p.evidence[0]) == 'INSTANT' for p in packets) and
        all(times) and max(times) - min(times) <= timedelta(hours=72) and
        len({p.universe.thesis_version_id for p in packets}) == 1 and
        len({s.direction for s in screens}) == 1 and
        len({s.thesis_impact for s in screens}) == 1 and all(eligible(s) for s in screens))


def components(db, checked):
    parents = {}
    def root(key):
        parents.setdefault(key, key)
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key
    for raw, digest in db.execute('SELECT payload,payload_hash FROM attention_event_groups ORDER BY rowid'):
        group = checked(raw, digest)
        keys = group['event_keys']
        for key in keys[1:]:
            parents[root(key)] = root(keys[0])
    return {key: root(key) for key in parents}


def persist_groups(db, groups, research, scan_id):
    from .attention import AttentionItem
    current = components(db, research.checked)
    for group in groups:
        roots = {current.get(key, key) for key in group.event_keys}
        keys = set(group.event_keys) | {k for k, v in current.items() if v in roots}
        members = []
        for key in sorted(keys):
            row = db.execute('SELECT payload,payload_hash FROM attention_items WHERE item_id=?', (key,)).fetchone()
            if not row:
                raise LLMError('invalid_event_group', 'Missing eligible event member')
            members.append(AttentionItem.model_validate(research.checked(*row)))
        if not compatible_members([i.packet for i in members], [i.screen for i in members]):
            raise LLMError('invalid_event_group', 'Transitive grouping crosses a protected boundary')
        payload = {**group.model_dump(mode='json'), 'scan_id': scan_id,
                   'contract': 'attention-research-event-v1'}
        key = content_hash(payload)
        db.execute('INSERT OR IGNORE INTO attention_event_groups VALUES (?,?,?)',
                   (key, json.dumps(payload, ensure_ascii=False), content_hash(payload)))
        current = components(db, research.checked)


def review_units(research, now):
    from .attention import AttentionItem, rank_key
    entries = {e.symbol: e for e in research.entries(now) if e.state != 'ARCHIVED'}
    with research.store._connect() as db:
        rows = db.execute('SELECT payload,payload_hash FROM attention_items ORDER BY rowid').fetchall()
        reviewed = {r[0] for r in db.execute('SELECT item_id FROM attention_reviewed')}
        groups = components(db, research.checked)
        bases = [research.checked(raw, digest) for raw, digest in db.execute(
            'SELECT payload,payload_hash FROM attention_event_groups ORDER BY rowid')]
    items = [AttentionItem.model_validate(research.checked(*r)) for r in rows]
    units = {}
    for item in items:
        if item.symbol in entries:
            units.setdefault(groups.get(item.item_id, item.item_id), []).append(item)
    output = []
    for members in units.values():
        unresolved = [i for i in members if i.item_id not in reviewed]
        if not unresolved:
            continue
        representative = max(unresolved, key=lambda i: rank_key(i.screen, entries[i.symbol], i.event_time, i.item_id))
        evidence = {}
        for item in sorted(members, key=lambda i: (i.event_time, i.item_id), reverse=True):
            for receipt in item.packet.evidence:
                if receipt.evidence_id in evidence and evidence[receipt.evidence_id] != receipt:
                    raise TDBError('ATTENTION_EVIDENCE_ID_COLLISION', 409)
                evidence[receipt.evidence_id] = receipt
        output.append({'item': representative.model_dump(mode='json'),
            'current_universe': entries[representative.symbol].model_dump(mode='json'),
            'deep': research.latest_deep(representative.item_id),
            'research_event': {'member_item_ids': [i.item_id for i in unresolved],
                'all_member_item_ids': [i.item_id for i in members],
                'evidence': [e.model_dump(mode='json') for e in evidence.values()],
                'grouped': len(members) > 1, 'identity_basis': 'CITED_SCREEN_PROPOSAL' if len(members) > 1 else 'SINGLE_RECEIPT',
                'grouping_records': [g for g in bases if set(g['event_keys']) & {i.item_id for i in members}],
                'revision_of': representative.packet.revision_of}})
        row = output[-1]
        row['deep'] = project_deep(research, row['deep'], row)
    output.sort(key=lambda row: rank_key(AttentionItem.model_validate(row['item']).screen,
        entries[row['item']['symbol']], AttentionItem.model_validate(row['item']).event_time,
        row['item']['item_id']), reverse=True)
    return output


def freeze_row(research, row):
    from .domain import EvidenceItem
    item = research.item(row['item']['item_id'])
    if not row['research_event']['grouped']:
        return item
    primary = item.packet.evidence[0]
    receipts = [primary] + [EvidenceItem.model_validate(e) for e in row['research_event']['evidence']
                           if e['evidence_id'] != primary.evidence_id]
    packet = item.packet.model_copy(update={'evidence': receipts,
        'cutoff': max(research.item(k).packet.cutoff for k in row['research_event']['all_member_item_ids'])})
    return item.model_copy(update={'packet': packet})


def related_candidates(research, packets, now, limit):
    from .attention import AttentionItem
    # Lexical overlap only selects bounded context; it never groups/suppresses.
    stop = {'the', 'and', 'with', 'from', 'after', 'that', 'this', 'company', 'news', 'reports',
            'report', 'stock', 'shares', 'market', 'says', 'said', 'will', 'have', 'has', 'for'}
    def tokens(e):
        return set(re.findall(r'[a-z][a-z0-9]{2,}', ((e.title or '') + ' ' + (e.excerpt or '')).casefold())) - stop
    candidates = []
    for row in review_units(research, now):
        for key in row['research_event']['member_item_ids']:
            item = research.item(key)
            if item.packet.revision_of or not item.packet.evidence[0].published_at:
                continue
            overlap = max((len(tokens(p.evidence[0]) & tokens(item.packet.evidence[0]))
                for p in packets if p.universe.symbol == item.symbol and
                abs(p.evidence[0].published_at - item.event_time) <= timedelta(hours=72)), default=0)
            if overlap >= 2:
                candidates.append((overlap, item.event_time, key, item))
    candidates.sort(key=lambda r: r[:3], reverse=True)
    return [r[3] for r in candidates[:limit]]
