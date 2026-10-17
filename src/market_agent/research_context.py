"""Bounded task context over existing SQLite receipts; no new memory backend."""
import json

from .market_evidence import market_identity
from .llm import LLMError
from .tdb import content_hash

MAX_CONTEXT_BYTES = 24000
MAX_SOURCES = 18


def build_context(task, *, supplemental_allowed, investigation=None):
    """Prefer fresh company facts and contrary primary evidence over old prose.

    Original receipts stay in ResearchTask. The prompt is explicitly a bounded
    projection; omission never means a source or unresolved fact does not exist.
    """
    rank = {'USER_ASSERTION': 0, 'OWNER_CONFIRMED_THESIS': 3, 'VERIFIED_FACT': 2,
            'AI_DRAFT': 5, 'HISTORICAL_DECISION': 6, 'UNKNOWN': 7}

    def priority(source):
        evidence = source.evidence
        if source.identity == 'VERIFIED_FACT' and source.freshness == 'CURRENT':
            value = 1 if evidence.evidence_type in {'news', 'sec_filing'} else 2
        else:
            value = rank[source.identity] + (2 if source.freshness == 'STALE' else 0)
        # New information from the one permitted investigation gets priority.
        if investigation and evidence.evidence_id in investigation.get('new_evidence_ids', []):
            value = 1
        return value, -(evidence.published_at or evidence.as_of or evidence.retrieved_at).timestamp(), evidence.evidence_id

    fast = getattr(task,'mode','DEEP') == 'FAST'
    max_bytes, max_sources = (12000,8) if fast else (MAX_CONTEXT_BYTES,MAX_SOURCES)
    supplemental_allowed = supplemental_allowed and not fast
    facts = {'question': task.question, 'objective': task.objective, 'symbol': task.focus_symbol,
        'research_mode': 'FAST' if fast else 'DEEP',
        'evidence_cutoff': task.cutoff.isoformat(), 'premise': task.premise,
        'portfolio': task.portfolio_context, 'limitations': task.limitations,
        'sources': [], 'supplemental_allowed': supplemental_allowed,
        'budget': {'remaining_investigations': int(supplemental_allowed), 'remaining_assessments': int(supplemental_allowed),
            'max_context_bytes': max_bytes, 'tools': [] if fast else ['news.search.targeted', 'sec.search.supplemental']},
        'task_history': {'prior_summary': (task.findings_summary or '')[:600],
            'prior_summary_identity': 'AI_DRAFT', 'unresolved_question': (task.unresolved_question or '')[:600]},
        'investigation': investigation}
    selected, manifest = [], []
    for source in sorted(task.sources, key=priority):
        evidence = source.evidence
        # Expired business receipts remain inspectable in immutable history.
        # Do not fill an otherwise small current-event prompt with stale search
        # hits; explicit historical/thesis comparisons can select background.
        if (source.freshness=='STALE' and evidence.evidence_type in {'news','sec_filing'}
                and not any(word in task.question for word in ('历史','此前','冲突','对比','historical','conflict'))):
            continue
        reason = ('material investigation result' if investigation and evidence.evidence_id in investigation.get('new_evidence_ids', []) else
            'current company evidence' if source.freshness == 'CURRENT' and evidence.evidence_type in {'news','sec_filing'} else
            'user premise' if source.identity == 'USER_ASSERTION' else
            'historical thesis; not new facts' if evidence.evidence_type == 'thesis' else 'scoped supporting context')
        row = source.model_dump(mode='json')
        row['evidence']['excerpt'] = (evidence.excerpt or '')[:1600] or None
        # Full calculation already appears in the typed portfolio field. Large
        # snapshots and historical source lists need not be sent twice.
        if evidence.evidence_type == 'portfolio_snapshot':
            row['evidence']['structured_data'] = {'snapshot_id': task.snapshot_id, 'account_id': task.account_id}
        elif evidence.evidence_type == 'calculation' and evidence.structured_data.get('calculation') != 'sec_quarterly_v1':
            row['evidence']['structured_data'] = task.portfolio_context
        elif len(json.dumps(row['evidence']['structured_data'], ensure_ascii=False).encode()) > 5000:
            row['evidence']['structured_data'] = {'projection_omitted': True,
                'reason': 'large structured payload; full receipt retained in task'}
        row['context_role'] = ('HISTORICAL' if source.freshness in {'HISTORICAL','STALE'} else
            'VERIFIED_SOURCE' if source.identity == 'VERIFIED_FACT' else source.identity)
        row['selection_reason'] = reason
        row['excerpt_truncated'] = bool(evidence.excerpt and len(evidence.excerpt) > 1600)
        if evidence.evidence_type in {'news','market_data'}:
            row['market_identity'] = market_identity(evidence, task.cutoff).model_dump(mode='json')
        tentative = {**facts, 'sources': [*facts['sources'], row]}
        if len(selected) >= max_sources or len(json.dumps(tentative,ensure_ascii=False,default=str).encode()) > max_bytes:
            continue
        facts = tentative
        selected.append(source)
        manifest.append({'evidence_id': evidence.evidence_id, 'identity': source.identity,
            'freshness': source.freshness, 'reason': reason, 'excerpt_truncated': row['excerpt_truncated']})
    encoded = json.dumps(facts,ensure_ascii=False,default=str).encode()
    if len(encoded) > max_bytes or not selected:
        raise LLMError('research_context_budget', 'required research context exceeds bounded projection')
    audit = {'version':'bounded-research-v05','selected':manifest,
        'omitted_evidence_ids':[s.evidence.evidence_id for s in task.sources if s not in selected],
        'context_bytes':len(encoded), 'max_context_bytes':max_bytes, 'projection_hash':content_hash(facts)}
    return facts, selected, audit
