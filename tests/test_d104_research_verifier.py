"""Offline adversarial verification boundary; no live model quality claim."""
import asyncio
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from market_agent.domain import EvidenceItem
from market_agent.research_task import (ResearchAssessment, ResearchSource,
    ResearchStatement, validate_assessment)
from market_agent.research_verifier import (VerificationContext, VerificationDecision,
    VerifierClaim, VerifierEvidence, VerifierReceipt, verify_candidate, verify_with_revision)

NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)
TEXT = ('Synthetic issuer disclosure: revenue is 100 USD. Cash flows include both '
        'continuing and discontinued operations. Continuing-only cash flow is not disclosed.')


def context(text='Continuing-only cash flow is 100 USD.'):
    return VerificationContext(task_id='synthetic-d104', revision=3,
        question='Can continuing-only cash flow be determined?', cutoff=NOW,
        evidence=[VerifierEvidence(evidence_id='Epublic', text=TEXT,
            content_hash='synthetic-public-hash', published_at=NOW,
            source_uri='https://synthetic.invalid/disclosure', scope='SYNTHETIC')],
        claims=[VerifierClaim(claim_id='C1', text=text, evidence_ids=['Epublic'])],
        numeric_fact_tokens=['100'])


def original_numeric_validator(value):
    """Execute the actual production validator, rather than mimic numeric rules."""
    evidence = EvidenceItem(evidence_id='Epublic', evidence_type='sec_filing',
        provider='sec_edgar', symbol='APP', title='Synthetic public evidence',
        source_uri='https://synthetic.invalid/disclosure', published_at=NOW,
        retrieved_at=NOW, content_hash='synthetic-public-hash', excerpt=TEXT)
    source = ResearchSource(identity='VERIFIED_FACT', freshness='CURRENT', evidence=evidence)
    statement = ResearchStatement(text=value.claims[0].text, identity='AI_DRAFT', evidence_ids=['Epublic'])
    assessment = ResearchAssessment(disposition='RESEARCH_ONLY', core_judgment=statement,
        key_reasons=[statement], support=[], counterevidence=[statement],
        action_conditions=[statement], next_checks=[statement])
    validate_assessment(assessment, [source], objective='EARNINGS_CHECK')


class SemanticBoundary:
    """Synthetic semantic judge; decisions are fixtures, not a deployed LLM."""
    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        text = request.facts['claims'][0]['text']
        if 'is 100 USD' in text:
            return VerificationDecision.model_validate({'decision':'REVISE', 'issues':[{
                'code':'PERIOD_SCOPE', 'claim_id':'C1', 'evidence_ids':['Epublic'],
                'explanation':'Consolidated cash flow cannot establish continuing-only cash flow.'}]})
        return VerificationDecision(decision='ACCEPT')


def test_planted_scope_error_requires_independent_call_and_one_shared_repair():
    boundary = SemanticBoundary(); claims = []; repairs = []
    def claim_repair():
        claims.append(True)
        return len(claims) == 1
    async def repair(value, outcome):
        repairs.append(outcome)
        return value.model_copy(update={'claims':[VerifierClaim(claim_id='C1',
            text='Continuing-only cash flow cannot be determined from the supplied disclosure.',
            evidence_ids=['Epublic'])]})
    original = context()
    run = asyncio.run(verify_with_revision(original, complete_structured=boundary,
        deterministic_validate=original_numeric_validator, claim_repair=claim_repair, revise=repair))
    assert run.decision == 'ACCEPT' and run.repairs_used == 1
    assert [row.result.decision for row in run.attempts] == ['REVISE','ACCEPT']
    assert len(boundary.requests) == 2 and len(repairs) == len(claims) == 1
    assert original.claims[0].text == 'Continuing-only cash flow is 100 USD.'
    assert run.attempts[0].candidate_hash != run.attempts[1].candidate_hash
    assert set(boundary.requests[0].facts) == {'task_id','revision','question','cutoff',
        'evidence','claims','receipts','numeric_fact_tokens'}
    assert not boundary.requests[0].grounded_responses
    assert boundary.requests[0].purpose == 'research_independent_verifier_v1'


@pytest.mark.parametrize('spent', [False, True])
def test_repeated_bad_scope_abstains_without_second_repair(spent):
    boundary = SemanticBoundary(); repairs = []
    async def repeat(value, outcome):
        repairs.append(outcome)
        return value
    run = asyncio.run(verify_with_revision(context(), complete_structured=boundary,
        deterministic_validate=original_numeric_validator, claim_repair=lambda: not spent, revise=repeat))
    assert run.decision == 'ABSTAIN'
    assert len(repairs) == (0 if spent else 1)
    assert len(boundary.requests) == 1  # unchanged repair cannot become best-of-n
    assert run.repairs_used == (0 if spent else 1)
    assert run.attempts[0].result.decision == 'REVISE'


def test_original_numeric_validator_veto_prevents_model_accept_or_paid_send():
    boundary = SemanticBoundary()
    outcome = asyncio.run(verify_candidate(context('Cash flow is 999 USD.'),
        complete_structured=boundary, deterministic_validate=original_numeric_validator))
    assert outcome.result.decision == 'REVISE' and outcome.callback_attempts == 0
    assert outcome.deterministic_error == 'unsupported_numeric_fact'
    assert not boundary.requests


@pytest.mark.parametrize('scope', ['OWNER_CONTEXT','DISPLAY_ONLY_MARKET','RESTRICTED_NEWS'])
def test_private_and_display_only_scopes_cannot_enter_verifier_context(scope):
    raw = context().model_dump(); raw['evidence'][0]['scope'] = scope
    with pytest.raises(ValidationError):
        VerificationContext.model_validate(raw)


@pytest.mark.parametrize('change', ['future','unknown_citation','duplicate'])
def test_pit_and_citation_identity_fail_before_model_call(change):
    raw = context().model_dump()
    if change == 'future':
        raw['evidence'][0]['published_at'] = datetime(2026, 11, 11, tzinfo=timezone.utc)
    elif change == 'unknown_citation':
        raw['claims'][0]['evidence_ids'] = ['Eforeign']
    else:
        raw['evidence'].append(raw['evidence'][0])
    with pytest.raises(ValidationError):
        VerificationContext.model_validate(raw)


@pytest.mark.parametrize('fault', ['unknown_ref','reserved_issue','malformed','failure'])
def test_verifier_boundary_errors_abstain_without_retry_or_raw_exception(fault):
    calls = []
    async def bad(request):
        calls.append(request)
        if fault == 'failure':
            raise RuntimeError('sensitive-original-request-must-not-be-recorded')
        if fault == 'malformed':
            return {'decision':'ACCEPT'}
        return VerificationDecision.model_validate({'decision':'REVISE','issues':[{
            'code':'VERIFIER_UNAVAILABLE' if fault=='reserved_issue' else 'CITATION',
            'claim_id':'C1','evidence_ids':['Eforeign' if fault=='unknown_ref' else 'Epublic'],
            'explanation':'Synthetic boundary fault.'}]})
    outcome = asyncio.run(verify_candidate(context(), complete_structured=bad,
        deterministic_validate=original_numeric_validator))
    assert outcome.result.decision == 'ABSTAIN' and len(calls) == 1
    assert outcome.failure_code == 'verification_boundary_failed'
    assert 'sensitive' not in outcome.model_dump_json()


def test_repair_cannot_substitute_task_or_approved_evidence_packet():
    async def illegal(value, outcome):
        return value.model_copy(update={'task_id':'another-task'})
    run = asyncio.run(verify_with_revision(context(), complete_structured=SemanticBoundary(),
        deterministic_validate=original_numeric_validator, claim_repair=lambda: True, revise=illegal))
    assert run.decision == 'ABSTAIN' and run.repairs_used == 1
    assert len(run.attempts) == 2 and run.attempts[1].callback_attempts == 0
    assert run.final_context.task_id == 'synthetic-d104'


def test_no_reference_answer_or_research_role_reasoning_allowed_in_context():
    for field in ('reference_answer','research_reasoning','portfolio_context','api_key'):
        raw = context().model_dump(); raw[field] = 'forbidden'
        with pytest.raises(ValidationError):
            VerificationContext.model_validate(raw)


def test_receipt_pairing_and_period_metadata_survive_independent_context():
    raw = context().model_dump()
    raw['receipts'] = [VerifierReceipt(receipt_id='Ncash',evidence_id='Epublic',field='cash_flow',
        value='100',unit='usd',period_start='2026-01-01',period_end='2026-09-30',basis='REPORTED').model_dump()]
    raw['claims'][0]['receipt_ids'] = ['Ncash']
    checked = VerificationContext.model_validate(raw)
    assert checked.receipts[0].period_start == '2026-01-01'
    raw['claims'][0]['evidence_ids'] = []
    with pytest.raises(ValidationError,match='unknown_or_unpaired_candidate_receipt'):
        VerificationContext.model_validate(raw)


def test_inplace_repair_mutation_cannot_change_the_retained_packet():
    original = context()
    async def mutate(value, outcome):
        value.evidence.clear()
        return value
    run = asyncio.run(verify_with_revision(original,complete_structured=SemanticBoundary(),
        deterministic_validate=original_numeric_validator,claim_repair=lambda:True,revise=mutate))
    assert run.decision == 'ABSTAIN'
    assert len(original.evidence) == len(run.final_context.evidence) == 1


def test_python_receipt_provenance_needs_real_source_binding_without_invented_publication():
    raw = context().model_dump()
    raw['evidence'].append(VerifierEvidence(evidence_id='Ecalc',text='Deterministic receipt from public source.',
        content_hash='synthetic-calculation-hash',scope='SYNTHETIC',kind='DETERMINISTIC_CALCULATION',
        derived_from=['Epublic']).model_dump())
    checked = VerificationContext.model_validate(raw)
    assert checked.evidence[1].published_at is None and checked.evidence[1].source_uri is None
    raw['evidence'][1]['derived_from'] = ['Eprivate-or-absent']
    with pytest.raises(ValidationError,match='unbound_verification_calculation'):
        VerificationContext.model_validate(raw)


@pytest.mark.parametrize('permission',[None,1,'GO'])
def test_shared_repair_requires_exact_true_not_truthy_authority(permission):
    boundary = SemanticBoundary()
    async def forbidden(value, outcome):
        raise AssertionError('No revision without a genuine shared allowance')
    run = asyncio.run(verify_with_revision(context(),complete_structured=boundary,
        deterministic_validate=original_numeric_validator,claim_repair=lambda:permission,revise=forbidden))
    assert run.decision == 'ABSTAIN' and run.repairs_used == 0
    assert len(boundary.requests) == 1
