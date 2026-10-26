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
        if source.identity=='OWNER_CONFIRMED_THESIS' and task.objective=='REVIEW_THESIS':
            value = 0  # The comparison target must precede repeated company prose.
        elif source.identity=='VERIFIED_FACT' and evidence.structured_data.get('calculation')=='task_valuation_v1':
            value = 1 if source.freshness=='CURRENT' else 4
        elif source.identity == 'VERIFIED_FACT' and source.freshness == 'CURRENT':
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
    selected, manifest, omitted = [], [], []
    excerpt_owners = {}
    title_owners = {}

    def omit(source, reason):
        evidence = source.evidence
        omitted.append({'evidence_id':evidence.evidence_id, 'identity':source.identity,
            'source_hash':evidence.content_hash, 'source_uri':evidence.source_uri,
            'published_at':evidence.published_at.isoformat() if evidence.published_at else None,
            'as_of':evidence.as_of.isoformat() if evidence.as_of else None, 'reason':reason})
    for source in sorted(task.sources, key=priority):
        evidence = source.evidence
        if (evidence.published_at and evidence.published_at > task.cutoff) or (evidence.as_of and evidence.as_of > task.cutoff):
            omit(source, 'PIT cutoff excludes future source')
            continue
        if evidence.evidence_type not in {'news','sec_filing'} and evidence.retrieved_at > task.cutoff:
            omit(source, 'Not known at PIT cutoff')
            continue
        if evidence.symbol and evidence.symbol != task.focus_symbol and evidence.evidence_type in {'news','sec_filing','thesis','semantic_judgment'}:
            omit(source, 'Different focus symbol')
            continue
        # Expired business receipts remain inspectable in immutable history.
        # Do not fill an otherwise small current-event prompt with stale search
        # hits; explicit historical/thesis comparisons can select background.
        if (source.freshness=='STALE' and evidence.evidence_type in {'news','sec_filing'}
                and not any(word in task.question for word in ('历史','此前','冲突','对比','historical','conflict'))):
            omit(source, 'Stale company receipt is not current evidence')
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
        elif evidence.evidence_type == 'calculation' and evidence.structured_data.get('calculation') not in {'sec_quarterly_v1','task_valuation_v1'}:
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
            if evidence.evidence_type == 'news':
                repeated = {'symbol','provider','source_url','evidence_id','source_type',
                    'published_at','market_as_of','retrieved_at'}
                row['market_identity'] = {k:v for k,v in row['market_identity'].items() if k not in repeated}
                row['market_identity']['identity_and_times_in'] = 'evidence'
                # Identity/freshness are already explicit on this row; selection
                # rationale remains in the audit manifest rather than per-copy prose.
                row.pop('context_role',None)
                row.pop('selection_reason',None)
        # Share only byte-identical prose, never event identity. Each original's
        # ID, URL, time, hash and distinct metadata remain in the projection.
        excerpt = row['evidence']['excerpt']
        if evidence.evidence_type == 'news' and excerpt and not row['excerpt_truncated']:
            row['evidence'] = {k:v for k,v in row['evidence'].items() if v is not None}
            if row['evidence']['structured_data'].get('text') == excerpt:
                row['evidence']['structured_data'] = {
                    **{k:v for k,v in row['evidence']['structured_data'].items() if k != 'text'},
                    'text_in_excerpt': True}
            if excerpt in excerpt_owners:
                row['evidence']['excerpt'] = None
                row['excerpt_text_reference'] = excerpt_owners[excerpt]
            if evidence.title and evidence.title in title_owners:
                row['evidence'].pop('title',None)
                row['title_text_reference'] = title_owners[evidence.title]
        tentative = {**facts, 'sources': [*facts['sources'], row]}
        if len(selected) >= max_sources or len(json.dumps(tentative,ensure_ascii=False,default=str).encode()) > max_bytes:
            omit(source, 'Source-count limit' if len(selected) >= max_sources else 'Context byte budget')
            continue
        facts = tentative
        selected.append(source)
        if evidence.evidence_type == 'news' and excerpt and not row['excerpt_truncated']:
            excerpt_owners.setdefault(excerpt,evidence.evidence_id)
            if evidence.title:
                title_owners.setdefault(evidence.title,evidence.evidence_id)
        manifest.append({'evidence_id': evidence.evidence_id, 'identity': source.identity,
            'freshness': source.freshness, 'source_hash':evidence.content_hash,
            'source_uri':evidence.source_uri, 'published_at':evidence.published_at.isoformat() if evidence.published_at else None,
            'as_of':evidence.as_of.isoformat() if evidence.as_of else None, 'reason': reason, 'excerpt_truncated': row['excerpt_truncated']})
    encoded = json.dumps(facts,ensure_ascii=False,default=str).encode()
    if len(encoded) > max_bytes or not selected:
        raise LLMError('research_context_budget', 'required research context exceeds bounded projection')
    audit = {'version':'bounded-research-v06','selected':manifest,
        'omitted_evidence_ids':[s.evidence.evidence_id for s in task.sources if s not in selected],
        'omitted':omitted, 'cutoff':task.cutoff.isoformat(),
        'context_bytes':len(encoded), 'max_context_bytes':max_bytes, 'projection_hash':content_hash(facts)}
    return facts, selected, audit
