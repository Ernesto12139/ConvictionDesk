"""Synthetic text at an invented SEC identity; no downloaded filing or transport."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from market_agent.domain import EvidenceItem
from market_agent.evidence_action import build_evidence_packet, contract_request
from market_agent.llm import LLMError
from market_agent.research_task import ResearchSource
from market_agent.retrieval_windows import extend_source_windows
from market_agent.tdb import content_hash


def packet():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    text = ('Synthetic test passage. This is not an actual issuer filing. ' * 30)
    evidence = EvidenceItem(evidence_id='synthetic-window', evidence_type='sec_filing',
        provider='sec_edgar', symbol='XMPL', title='SYNTHETIC ONLY',
        source_uri='https://www.sec.gov/Archives/edgar/data/1/000000000126000001/synthetic.htm',
        published_at=now, as_of=now, retrieved_at=now, excerpt=text,
        content_hash=content_hash(text), structured_data={
            'cik': '0000000001', 'accession_number': '0000000001-26-000001'})
    source = ResearchSource(identity='VERIFIED_FACT', freshness='CURRENT', evidence=evidence)
    task = SimpleNamespace(question='Explain the synthetic passage', objective='EARNINGS_CHECK',
        focus_symbol='XMPL', cutoff=now, mode='DEEP', portfolio_context={}, premise={},
        limitations=[], investigation={}, sources=[source])
    value = build_evidence_packet(task)
    manifest = dict(source='sec_edgar', evidence_id=evidence.evidence_id,
        source_hash=evidence.content_hash, category='PUBLIC_FILINGS',
        allowed_fields=['excerpt'], license_reference='PUBLIC_SEC_FILINGS',
        citation_scope='PRIVATE_RESEARCH_ONLY',
        projection_hash=content_hash(evidence.model_dump(mode='json')))
    return value, source, manifest


def test_bounded_same_source_keeps_original_refs_receipts_and_packet():
    value, source, manifest = packet()
    original = value.model_dump(mode='json')
    request = contract_request(value)
    result = extend_source_windows(value, request, source, manifest)
    assert len(result.windows) == 2
    assert sum(len(window.text) for window in result.windows) <= 1200
    assert result.windows[0].text + result.windows[1].text == source.evidence.excerpt[:1200]
    assert {window.parent_ref for window in result.windows} == {value.refs[0].ref}
    assert result.source_group_count == 1
    assert result.request['facts']['numeric_receipts'] == request['facts']['numeric_receipts']
    assert result.request['evidence_ids'] == request['evidence_ids']
    assert result.final_facts_hash == content_hash(result.request['facts'])
    assert value.model_dump(mode='json') == original
    assert request == contract_request(value)


@pytest.mark.parametrize('fault', ['rights', 'source_hash', 'projection_hash', 'base'])
def test_unapproved_or_changed_binding_is_rejected(fault):
    value, source, manifest = packet()
    request = deepcopy(contract_request(value))
    if fault == 'rights':
        manifest['allowed_fields'] = []
    elif fault == 'base':
        request['facts']['question'] = 'Changed after approval'
    else:
        manifest[fault] = 'changed'
    with pytest.raises(LLMError, match='Bounded source window binding failed'):
        extend_source_windows(value, request, source, manifest)
