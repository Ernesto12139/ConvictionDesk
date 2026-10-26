"""Server-owned evidence plan shared by Ask and selected Today events.

The model proposes typed intent/gaps; this policy owns permissions and budgets.
It never accepts executable names, URLs, account IDs or cutoffs from model text.
"""
from datetime import datetime
from typing import Literal

from pydantic import Field

from .domain import FrozenModel, utc

ToolName = Literal['news.search', 'sec.search', 'sec.fundamentals']


class PlannerInput(FrozenModel):
    entry: Literal['ASK', 'TODAY']
    question: str = Field(min_length=1, max_length=1200)
    symbol: str
    objective: str
    account_id: str | None
    cutoff: datetime
    mode: Literal['FAST', 'DEEP']
    event_id: str | None = None
    evidence_ids: list[str] = []
    evidence_catalog: list[dict] = []


def ask_input(task):
    return PlannerInput(entry='TODAY' if task.attention_item_id else 'ASK',
        question=task.question, symbol=task.focus_symbol, objective=task.objective,
        account_id=task.account_id, cutoff=task.cutoff, mode=task.mode,
        event_id=task.attention_item_id, evidence_ids=[s.evidence.evidence_id for s in task.sources],
        evidence_catalog=[{'id':s.evidence.evidence_id,'kind':s.evidence.evidence_type,
            'identity':s.identity,'freshness':s.freshness,'hash':s.evidence.content_hash,
            'published_at':s.evidence.published_at.isoformat() if s.evidence.published_at else None}
            for s in task.sources])


def today_input(task, selected_item):
    """Adapter after the existing ResearchStore authorization/identity lookup."""
    if selected_item.item_id != task.attention_item_id or selected_item.symbol != task.focus_symbol or selected_item.packet.cutoff > task.cutoff:
        raise ValueError('Today planner context mismatch')
    return ask_input(task)


class ToolPlanItem(FrozenModel):
    tool: ToolName
    decision: Literal['RUN', 'SKIP']
    reason: str
    query: str | None = Field(default=None, max_length=240)
    expected_evidence: str
    source_qualification: str
    cutoff: datetime
    limit: int = Field(ge=0, le=5)
    cost_qualification: str = 'Existing provider transport authorization required; plan grants no paid permission'


class ToolPlan(FrozenModel):
    version: Literal['loop-001-v1'] = 'loop-001-v1'
    context: PlannerInput
    items: list[ToolPlanItem]
    max_supplements: Literal[0, 1]

    def runs(self, tool):
        return any(row.tool == tool and row.decision == 'RUN' for row in self.items)


def plan_tools(context, intent, *, news_available, sec_available, financials_available):
    """Compile validated semantic intent into a finite, explainable allowlist."""
    rows = []
    for name, wanted, available, expected, qualification in (
        ('news.search', intent.use_news and context.objective != 'EARNINGS_CHECK', news_available,
         'Recent company events and opposing reports', 'Symbol match; publication <= cutoff; existing news rights gate'),
        ('sec.search', intent.use_sec, sec_available,
         'Primary disclosure supporting or challenging the thesis', 'Symbol/forms/limit server-bound; publication <= cutoff'),
        ('sec.fundamentals', intent.use_sec and context.mode == 'DEEP' and context.objective in {'CONSIDER_ADD','CONSIDER_REDUCE','EARNINGS_CHECK','REVIEW_THESIS'}, financials_available,
         'Exact-period deterministic financial receipts', 'Existing SEC financial lineage and Decimal qualification; no model arithmetic')):
        covered = name == 'news.search' and sum(row['kind'] == 'news' and row['identity'] == 'VERIFIED_FACT' and row['freshness'] == 'CURRENT' for row in context.evidence_catalog) >= 2
        run = wanted and available and not covered
        reason = ('Reuse at least two eligible current news receipts; a cited material gap may reopen targeted news' if covered else 'Intent requires this evidence' if run else 'Provider unavailable' if wanted else
                  'Earnings investigation prioritizes primary filings; targeted news remains possible for a material gap' if name == 'news.search' and intent.use_news else
                  'Not needed for this objective/mode or semantic intent declined this source')
        rows.append(ToolPlanItem(tool=name, decision='RUN' if run else 'SKIP', reason=reason,
            query=intent.sec_query if name == 'sec.search' else 'exact-period financial facts' if name == 'sec.fundamentals' else None,
            expected_evidence=expected, source_qualification=qualification, cutoff=utc(context.cutoff),
            limit=(2 if context.mode == 'FAST' else 4 if name != 'news.search' else 5) if run else 0))
    return ToolPlan(context=context, items=rows, max_supplements=0 if context.mode == 'FAST' else 1)


def replan_stop(*, proposed, available, mode, blocked, calls, maximum=12):
    if blocked:
        return 'BUDGET_OR_TIMEOUT_STOP'
    if not proposed:
        return 'NO_MATERIAL_GAP_REQUESTED'
    if mode == 'FAST':
        return 'FAST_BUDGET_STOP'
    if calls >= maximum:
        return 'TOOL_BUDGET_STOP'
    if not available:
        return 'SOURCE_UNAVAILABLE'
    return None
