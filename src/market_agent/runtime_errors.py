"""Stable UI failure families; original machine codes remain available."""


def failure_kind(code):
    if code.startswith(('api_budget','experiment_','loop_tool_budget')):
        return 'BUDGET_BLOCKED'
    if code.startswith(('egress_','data_rights')):
        return 'DATA_RIGHTS_BLOCKED'
    if code in {'timeout','loop_tool_timeout','RESEARCH_TIMEOUT','RESEARCH_MODEL_TIMEOUT','PROVIDER_TIMEOUT'}:
        return 'PROVIDER_TIMEOUT'
    if code in {'RESEARCH_INTERRUPTED','cancelled'}:
        return 'CANCELLED'
    if code in {'numeric_receipt_required','receipt_missing','contract_projection_missing','ref_collision'}:
        return 'CONTRACT_PROJECTION_MISSING'
    if code.startswith(('unsupported_','invalid_evidence','stale_current','action_')) or code in {
            'symbol_mismatch','unauthorized_trade_quantity','insufficient_evidence','objective_mismatch',
            'insufficient_action_evidence','missing_investigation_effect','unknown_evidence_ref',
            'citation_required','unknown_numeric_receipt','receipt_not_cited','memo_too_long','position_required'}:
        return 'MODEL_UNSUPPORTED_CLAIM'
    return 'EVIDENCE_GAP' if code.startswith('evidence_gap') or code in {'evidence_conflict','research_context_budget'} else 'PROVIDER_ERROR'
