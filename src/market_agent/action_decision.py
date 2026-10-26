"""Deterministic applicability and readback of an evidence-validated direction."""
from decimal import Decimal
from typing import Literal

from .domain import FrozenModel
from .llm import LLMError


class ActionDecision(FrozenModel):
    symbol: str
    decision_action: Literal['ADD','HOLD','REDUCE','EXIT'] | None
    decision_status: Literal['SUPPORTED','PROVISIONAL','UNDETERMINED','NOT_APPLICABLE','NO_ACTION_REQUESTED']
    evidence_strength: Literal['STRONG','MIXED','LIMITED','INSUFFICIENT']
    thesis_delta: str
    key_drivers: list[str]
    counterevidence: list[str]
    change_conditions: list[str]
    source_ids: list[str]
    research_cutoff: str
    model_profile: str | None
    actual_model: str | None
    trace_id: str
    position_status: Literal['HELD','NOT_HELD','UNKNOWN']
    applicability_note: str | None = None


NO_ACTION_OBJECTIVES = {'EXPLAIN_CHANGE', 'EARNINGS_CHECK'}


def action_readback(assessment, task):
    action = assessment.action
    context = task.portfolio_context
    held = 'UNKNOWN'
    if context and context.get('snapshot_id'):
        position = context.get('position')
        held = ('HELD' if position and position.get('symbol') == task.focus_symbol
                and Decimal(str(position.get('quantity','0'))) > 0 else 'NOT_HELD')
    direction = action.direction if action else None
    status = action.status if action else 'UNDETERMINED'
    note = None
    if direction in {'HOLD','REDUCE','EXIT'} and held != 'HELD':
        direction = None
        status = 'NOT_APPLICABLE' if held == 'NOT_HELD' else 'UNDETERMINED'
        note = '所选持仓范围没有该标的，持有/减持/退出不适用。' if held == 'NOT_HELD' else '尚未取得该标的持仓身份。'
    if (direction is None and status == 'UNDETERMINED' and held == 'NOT_HELD'
            and getattr(task, 'objective', None) == 'CONSIDER_REDUCE'):
        # Reduce/exit without a position cannot apply. A thesis review of an unheld
        # symbol stays a research question (UNDETERMINED), per the frozen v09 contract.
        status = 'NOT_APPLICABLE'
        note = '所选持仓范围没有该标的，减持/退出不适用；这不是证据不足'
    if direction is None and status == 'UNDETERMINED' and getattr(task, 'objective', None) in NO_ACTION_OBJECTIVES:
        # A research answer, not missing evidence: the question asked for no trade direction.
        status = 'NO_ACTION_REQUESTED'
        note = '本次问题没有请求交易方向；以下是研究回答，不是证据不足。'
    return ActionDecision(symbol=task.focus_symbol, decision_action=direction, decision_status=status,
        evidence_strength=action.evidence_strength if action else 'INSUFFICIENT',
        thesis_delta=action.thesis_delta.text if action else '当前输出没有经过验证的 Thesis Delta。',
        key_drivers=[statement.text for statement in assessment.key_reasons],
        counterevidence=[statement.text for statement in assessment.counterevidence],
        change_conditions=[statement.text for statement in assessment.action_conditions],
        source_ids=list(dict.fromkeys(ref for statement in assessment.statements() for ref in statement.evidence_ids)),
        research_cutoff=task.cutoff.isoformat(), model_profile=getattr(task,'model_profile_id',None),
        actual_model=task.model_events[-1].model if task.model_events else None,
        trace_id=task.task_id, position_status=held, applicability_note=note)


def validate_action(action, sources):
    if action is None:
        return
    if (action.direction is None) != (action.status == 'UNDETERMINED'):
        raise LLMError('action_status_mismatch','Unknown direction must stay explicitly undetermined')
    if action.reason.identity not in {'AI_DRAFT','UNKNOWN'}:
        raise LLMError('unsupported_statement_identity','Direction is a judgment, never a verified fact')
    if action.direction:
        cited = [source for source in sources if source.evidence.evidence_id in action.reason.evidence_ids]
        if not any(source.identity == 'VERIFIED_FACT' and source.freshness == 'CURRENT'
                   and source.evidence.evidence_type in {'news','sec_filing'} for source in cited):
            raise LLMError('insufficient_action_evidence','A direction requires cited eligible company evidence')
        if action.evidence_strength == 'INSUFFICIENT':
            raise LLMError('action_status_mismatch','Insufficient evidence cannot authorize a direction')
        if action.status == 'SUPPORTED' and action.evidence_strength == 'LIMITED':
            raise LLMError('action_status_mismatch','Limited evidence supports a provisional direction only')
