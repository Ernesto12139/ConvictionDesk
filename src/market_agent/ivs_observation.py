"""Allowlisted observation projection for the IVS-001 research chain.

A pure, read-only adapter over the existing runtime records (``ResearchTask``,
``ToolCall``, ``SynthesisEvent``, ``ResearchSource`` and the typed receipts from
``evidence_action``). It never fetches evidence, never calls a model/provider,
never reads the Owner database and never raises the Validator. Its only job is
to answer: for one research run, was the right tool chosen, the necessary
information obtained, the source retained accurately, and the judgment clear and
applicable — without leaking restricted content.

The projection records only whitelisted metadata. Restricted news bodies, owner
portfolio values, account identities, consent tokens, secrets and raw model
payloads are replaced by a redaction marker plus a structured redaction log, so
the trace remains reproducible and auditable without exposing private content.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .domain import FrozenModel, utc
from .tdb import content_hash

OBSERVATION_SCHEMA = 'ivs-observation-v1'

# Receipt value is public only when it derives from a public filing or a market
# quote. Portfolio/calculation and user-assertion receipts are owner/sensitive.
TOKEN_FIELDS = {'input_tokens', 'output_tokens', 'total_tokens', 'reasoning_tokens',
                'cache_read_input_tokens', 'cache_creation_input_tokens'}

# Failure attribution: canonical family and disposition, kept in the source so a
# trace can be self-classifying without importing the eval package.
TRUE_MODEL_CODES = {'unsupported_numeric_fact', 'unsupported_statement_identity',
                    'symbol_mismatch', 'stale_current_claim', 'unauthorized_trade_quantity',
                    'action_status_mismatch', 'objective_mismatch',
                    'insufficient_action_evidence', 'position_required'}


def classify_failure(code):
    """Map a runtime error code to (family, disposition)."""
    if not code:
        return None, None
    if code in {'api_budget_cost', 'api_budget_pending'}:
        return 'BUDGET_BLOCKED', 'INFRASTRUCTURE'
    if code.startswith('egress_') or code.startswith('data_rights'):
        return 'DATA_RIGHTS_BLOCKED', 'INFRASTRUCTURE'
    if code in {'loop_tool_timeout', 'RESEARCH_TIMEOUT', 'PROVIDER_TIMEOUT'}:
        return 'PROVIDER_TIMEOUT', 'INFRASTRUCTURE'
    if code == 'numeric_receipt_required':
        return 'MODEL_UNSUPPORTED_CLAIM', 'TRUE_MODEL_FAILURE'
    if code in TRUE_MODEL_CODES:
        return 'MODEL_UNSUPPORTED_CLAIM', 'TRUE_MODEL_FAILURE'
    if code in {'NOT_APPLICABLE', 'NO_ACTION_REQUESTED'}:
        return 'ACTION_NOT_APPLICABLE', 'CORRECT_OUTCOME'
    return 'OTHER', 'TRUE_MODEL_FAILURE'


class ObservationEvidence(FrozenModel):
    evidence_id: str
    source_id: str | None = None
    accession: str | None = None
    evidence_type: str
    provider: str
    symbol: str | None = None
    identity: str
    freshness: str
    published_at: datetime | None = None
    as_of: datetime | None = None
    pit_ok: bool
    content_hash: str
    title: str | None = None
    title_redacted: bool = False
    excerpt_redacted: bool = False
    accepted_at: datetime | None = None
    time_precision: str | None = None
    source_url: str | None = None
    rights_decision: str = 'UNKNOWN'


class ObservationReceipt(FrozenModel):
    receipt_id: str
    evidence_id: str
    field: str
    unit: str
    as_of: datetime | None = None
    basis: str | None = None
    value: str | None = None
    value_redacted: bool = False


class ObservationTool(FrozenModel):
    sequence: int
    name: str
    status: str
    error: str | None = None
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    arguments: dict = Field(default_factory=dict)
    output_ref_count: int = 0


class ObservationModel(FrozenModel):
    model: str
    purpose: str
    status: str
    error: str | None = None
    latency_ms: int | None = None
    tokens: dict = Field(default_factory=dict)
    validation_outcome: str | None = None
    prompt_contract: str | None = None
    attempt: str | None = None


class ObservationRouting(FrozenModel):
    mode: str
    route_reason: str | None = None
    jev_status: str | None = None
    jev_objective: str | None = None
    jev_candidate: str | None = None


class ObservationConsent(FrozenModel):
    decision: Literal['AUTHORIZED', 'BLOCKED', 'UNKNOWN']
    category: str | None = None
    reason: str | None = None


class ObservationSupplement(FrozenModel):
    triggered: bool
    trigger: str | None = None
    new_evidence_ids: list[str] = Field(default_factory=list)


class ObservationAction(FrozenModel):
    direction: str | None = None
    status: str | None = None
    evidence_strength: str | None = None
    position_status: str
    applicable: bool


class ObservationFailure(FrozenModel):
    code: str
    family: str | None = None
    disposition: str | None = None
    retried: bool = False
    retry_outcome: str | None = None


class ObservationCost(FrozenModel):
    model_calls: int
    tool_calls: int
    total_latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: str | None = None
    cost_known: bool = False


class ObservationTrace(FrozenModel):
    schema_version: Literal['ivs-observation-v1'] = OBSERVATION_SCHEMA
    run_id: str
    correlation_id: str
    attention_item_id: str | None = None
    question_hash: str
    objective: str
    focus_symbol: str | None = None
    mode: str
    cutoff: datetime
    status: str
    error: str | None = None
    scope: dict = Field(default_factory=dict)
    evidence: list[ObservationEvidence] = Field(default_factory=list)
    receipts: list[ObservationReceipt] = Field(default_factory=list)
    receipt_status: Literal['PROJECTED', 'PROJECTION_FAILED'] = 'PROJECTED'
    tools: list[ObservationTool] = Field(default_factory=list)
    models: list[ObservationModel] = Field(default_factory=list)
    routing: ObservationRouting
    consent: ObservationConsent = Field(default_factory=lambda: ObservationConsent(decision='UNKNOWN'))
    supplement: ObservationSupplement = Field(default_factory=lambda: ObservationSupplement(triggered=False))
    action: ObservationAction | None = None
    failure: ObservationFailure | None = None
    cost: ObservationCost
    redactions: list[dict] = Field(default_factory=list)


_ALLOWED_ARG_KEYS = {'symbol', 'symbols', 'as_of', 'since', 'until', 'limit', 'forms'}


def _safe_arguments(call):
    args = call.arguments
    if args is None:
        return {}
    data = args.model_dump(mode='json') if hasattr(args, 'model_dump') else (args or {})
    safe = {}
    for key in _ALLOWED_ARG_KEYS:
        if key in data:
            value = data[key]
            safe[key] = value
    return safe


def _pit_ok(source, cutoff):
    evidence = source.evidence
    if evidence.published_at and evidence.published_at > cutoff:
        return False
    if evidence.as_of and evidence.as_of > cutoff:
        return False
    return True


def _redact(reason, *, field=None):
    row = {'reason': reason}
    if field:
        row['field'] = field
    return row


def _tag(value):
    """Keep machine codes, never arbitrary exception or routing prose."""
    import re
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]{0,79}', value) else None


def observe_task(task, *, egress_record=None, cost_usd=None, offline_scripted=False):
    """Project one research run into an allowlisted observation trace.

    ``egress_record`` is the decoded consent record (from ``ResearchEgress``) when
    available; otherwise consent is UNKNOWN. ``cost_usd`` is recorded only when a
    real, verified cost exists; ``None`` is unknown, never zero.
    """
    redactions: list[dict] = []
    cutoff = utc(task.cutoff)

    evidence_rows = []
    for source in task.sources:
        evidence = source.evidence
        # Titles and receipt values may contain owned data even on calculation/quote rows.
        title = None
        title_redacted = bool(evidence.title)
        excerpt_redacted = evidence.evidence_type in {'news', 'thesis', 'semantic_judgment'}
        if excerpt_redacted:
            redactions.append(_redact('restricted article/thesis prose excluded', field='excerpt'))
        data = evidence.structured_data or {}
        accepted = data.get('accepted_at')
        try:
            accepted = utc(datetime.fromisoformat(accepted.replace('Z', '+00:00'))) if isinstance(accepted, str) else None
        except ValueError:
            accepted = None
        from urllib.parse import urlsplit
        url = urlsplit(evidence.source_uri or '')
        public_sec = evidence.evidence_type == 'sec_filing' and evidence.provider == 'sec_edgar'
        source_url = evidence.source_uri if public_sec and url.scheme == 'https' and url.netloc == 'www.sec.gov' and url.path.startswith('/Archives/edgar/data/') and not url.query and not url.fragment else None
        evidence_rows.append(ObservationEvidence(
            evidence_id=evidence.evidence_id,
            source_id=evidence.source_record_id if public_sec else None,
            accession=evidence.source_record_id if public_sec else None,
            evidence_type=evidence.evidence_type, provider=evidence.provider,
            symbol=evidence.symbol, identity=source.identity, freshness=source.freshness,
            published_at=evidence.published_at, as_of=evidence.as_of,
            pit_ok=_pit_ok(source, cutoff), content_hash=evidence.content_hash,
            title=title, title_redacted=title_redacted, excerpt_redacted=excerpt_redacted,
            accepted_at=accepted, time_precision=data.get('time_precision') if data.get('time_precision') in {'SECOND','DATE','UNKNOWN'} else None,
            source_url=source_url, rights_decision='PUBLIC_METADATA_ONLY' if public_sec and data.get('claim_scope') == 'PUBLIC_DISCLOSURE_METADATA_ONLY_NOT_INVESTMENT_IMPACT' else 'UNKNOWN'))

    # Typed numeric receipts come from the existing projection; owner values redact.
    receipt_rows = []
    receipt_status = 'PROJECTED'
    try:
        from .evidence_action import build_evidence_packet
        packet = build_evidence_packet(task, sources=task.sources)
        for receipt in packet.receipts:
            redactions.append(_redact('numeric value excluded; immutable receipt identity retained', field=receipt.field))
            receipt_rows.append(ObservationReceipt(
                receipt_id=receipt.receipt_id, evidence_id=receipt.evidence_id,
                field=receipt.field, unit=receipt.unit, as_of=receipt.as_of,
                basis=None, value=None, value_redacted=True))
    except Exception:
        receipt_status = 'PROJECTION_FAILED'

    tools = []
    for call in task.tool_calls:
        tools.append(ObservationTool(
            sequence=call.sequence, name=call.name, status=call.status, error=_tag(call.error),
            started_at=call.started_at, ended_at=call.ended_at,
            duration_ms=max(0, round((call.ended_at - call.started_at).total_seconds() * 1000)),
            arguments=_safe_arguments(call), output_ref_count=len(call.output_refs)))

    models = []
    for event in task.model_events:
        models.append(ObservationModel(
            model=event.model, purpose=event.purpose, status=event.status, error=_tag(event.error),
            latency_ms=event.latency_ms, tokens={k: v for k, v in (event.usage or {}).items()
                                                 if k in TOKEN_FIELDS and type(v) is int and v >= 0},
            validation_outcome=event.validation_outcome,
            prompt_contract=getattr(event, 'prompt_contract', None),
            attempt=getattr(event, 'attempt', None)))

    loop = task.loop_trace or {}
    jev_routing = loop.get('jev_routing') or {}
    jev_candidate = loop.get('jev_candidate') or {}
    routing = ObservationRouting(
        mode=task.mode,
        route_reason=_tag(loop.get('stop_reason') or loop.get('fallback')),
        jev_status=_tag(jev_routing.get('status')),
        jev_objective=_tag((jev_routing.get('choices') or {}).get('objective')),
        jev_candidate=_tag(jev_candidate.get('candidate')))

    consent = ObservationConsent(decision='UNKNOWN')
    if egress_record:
        preview = egress_record.get('preview') or {}
        binding = preview.get('binding') or {}
        consent = ObservationConsent(
            # A historical confirmation alone proves neither current expiry nor binding.
            decision='BLOCKED' if (egress_record.get('revoked') or not egress_record.get('confirmed_at')) else 'UNKNOWN',
            category=(binding.get('provider') or 'jev'),
            reason='revoked or unconfirmed' if (egress_record.get('revoked') or not egress_record.get('confirmed_at')) else None)
        redactions.append(_redact('consent token excluded', field='consent_id'))

    investigation = task.investigation or {}
    supplement = ObservationSupplement(
        triggered=bool(investigation.get('gap') or investigation.get('status')),
        trigger=_tag(investigation.get('reason') or investigation.get('status')),
        new_evidence_ids=list(investigation.get('new_evidence_ids') or []))

    action = None
    if task.action_decision:
        decision = task.action_decision
        action = ObservationAction(
            direction=decision.decision_action, status=decision.decision_status,
            evidence_strength=decision.evidence_strength,
            position_status=decision.position_status,
            applicable=decision.decision_status in {'SUPPORTED', 'PROVISIONAL'} and decision.decision_action is not None)

    failure = None
    code = task.error or task.error_kind
    if code:
        family, disposition = classify_failure(code)
        failure = ObservationFailure(code=_tag(code) or 'REDACTED_ERROR', family=family, disposition=disposition)

    total_latency = sum(m.latency_ms for m in models if m.latency_ms is not None)
    usage = {}
    for event in task.model_events:
        for key, value in (event.usage or {}).items():
            if key in TOKEN_FIELDS and type(value) is int and value >= 0:
                usage[key] = usage.get(key, 0) + value

    cost = ObservationCost(
        model_calls=len(models), tool_calls=len(tools), total_latency_ms=total_latency,
        input_tokens=usage.get('input_tokens'), output_tokens=usage.get('output_tokens'),
        cost_usd=cost_usd, cost_known=cost_usd is not None)

    redactions.append(_redact('question text replaced by hash', field='question'))
    if task.portfolio_context:
        redactions.append(_redact('owner portfolio quantities/cash/cost excluded', field='portfolio_context'))

    return ObservationTrace(
        run_id=task.task_id,
        correlation_id=f'{task.task_id}:r{task.revision}',
        attention_item_id=task.attention_item_id,
        question_hash=content_hash(task.question),
        objective=task.objective, focus_symbol=task.focus_symbol, mode=task.mode,
        cutoff=cutoff, status=task.status, error=_tag(task.error),
        scope={'portfolio': 'REDACTED' if task.portfolio_context else 'NONE',
               'network': False if offline_scripted else 'UNKNOWN',
               'paid_dispatch': 'BLOCKED' if offline_scripted else 'UNKNOWN'},
        evidence=evidence_rows, receipts=receipt_rows, receipt_status=receipt_status, tools=tools, models=models,
        routing=routing, consent=consent, supplement=supplement, action=action,
        failure=failure, cost=cost, redactions=redactions)


def serialize_trace(trace: ObservationTrace) -> dict:
    """JSON-safe serialization for the handoff (used by the eval report)."""
    return trace.model_dump(mode='json')
