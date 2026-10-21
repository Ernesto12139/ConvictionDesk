"""Detached, single-account hypothetical fills. No providers, orders or writes."""
from datetime import date
from decimal import Decimal, localcontext
from typing import Annotated, Literal

from pydantic import Field, field_validator

from .domain import FrozenModel
from .tdb import content_hash

Amount = Annotated[Decimal, Field(ge=0, le=Decimal('1e12'), max_digits=24, decimal_places=10)]


class WhatIfRequest(FrozenModel):
    revision: int = Field(ge=1)
    account_id: str = Field(min_length=1, max_length=100)
    symbol: str = Field(pattern=r'^[A-Z][A-Z0-9.\-]{0,14}$')
    side: Literal['BUY', 'SELL']
    shares: Amount
    fill_price: Amount
    reference_mark: Amount | None = None
    fee: Amount = Decimal(0)
    currency: Literal['USD'] = 'USD'
    scenario_date: date
    unchanged_share_basis_attested: bool = False
    # These are hypothetical marks, never quotes. Omitted marks remain unknown.
    other_prices: dict[str, Amount] = Field(default_factory=dict, max_length=100)

    @field_validator('shares', 'fill_price', 'fee', 'reference_mark', mode='before')
    @classmethod
    def exact(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError('Use exact decimal strings')
        if isinstance(value, str) and ('e' in value.lower()):
            raise ValueError('Use ordinary decimal notation, not scientific notation')
        return value

    @field_validator('other_prices', mode='before')
    @classmethod
    def exact_marks(cls, value):
        if isinstance(value, dict) and any(isinstance(v, (float, bool)) for v in value.values()):
            raise ValueError('Use exact decimal strings for hypothetical marks')
        if isinstance(value, dict) and any(isinstance(v, str) and 'e' in v.lower() for v in value.values()):
            raise ValueError('Use ordinary decimal notation for hypothetical marks')
        return value


def plain(value):
    """Exact ordinary decimal text: never exponent form, never a signed zero."""
    return format(abs(value) if value == 0 else value, 'f')


class WhatIfUnavailable(ValueError):
    """A safe, public refusal code; no private exception text."""


def scope(task, snapshot):
    if (task.status != 'COMPLETE' or not task.action_decision or not snapshot
            or snapshot.snapshot_id != task.snapshot_id or snapshot.content_hash != task.snapshot_hash
            or snapshot.as_of > task.cutoff or snapshot.source.imported_at > task.cutoff):
        raise WhatIfUnavailable('SAVED_ACTION_AND_MATCHING_PIT_SNAPSHOT_REQUIRED')
    return {'snapshot_id': snapshot.snapshot_id, 'snapshot_hash': snapshot.content_hash,
        'snapshot_as_of': snapshot.as_of.isoformat(), 'research_cutoff': task.cutoff.isoformat(),
        'symbol': task.focus_symbol, 'revision': task.revision,
        'accounts': [{'account_id': a.account_id, 'name': a.name, 'cash': plain(a.cash),
            'positions': [{'symbol': p.symbol, 'shares': plain(p.quantity)} for p in snapshot.positions
                          if p.account_id == a.account_id]}
            for a in snapshot.accounts if not task.account_id or a.account_id == task.account_id]}


def simulate(task, snapshot, request):
    context = scope(task, snapshot)
    if request.revision != task.revision or request.symbol != task.focus_symbol:
        raise WhatIfUnavailable('TASK_INSTRUMENT_OR_REVISION_MISMATCH')
    if request.account_id not in {a['account_id'] for a in context['accounts']}:
        raise WhatIfUnavailable('ACCOUNT_OUTSIDE_SAVED_SCOPE')
    if not request.unchanged_share_basis_attested:
        raise WhatIfUnavailable('SHARE_BASIS_CONTINUITY_ATTESTATION_REQUIRED')
    if request.scenario_date < snapshot.as_of.date():
        raise WhatIfUnavailable('SCENARIO_PREDATES_SNAPSHOT')
    for source in task.sources:
        data = source.evidence.structured_data or {}
        if source.evidence.symbol != task.focus_symbol:
            continue
        classes = data.get('listed_common_symbols')
        listing = data.get('issuer_listing_identity', {})
        check = data.get('capital_basis_check', {})
        if (isinstance(classes, list) and classes != [task.focus_symbol]
                or isinstance(listing, dict) and isinstance(listing.get('tickers'), list)
                    and listing['tickers'] != [task.focus_symbol]
                or isinstance(check, dict) and (check.get('split_status') == 'CHANGE_DETECTED'
                    or check.get('share_count_status') == 'CHANGE_DETECTED'
                    or check.get('share_class_alignment') == 'MISMATCH')):
            raise WhatIfUnavailable('KNOWN_CAPITAL_OR_SHARE_CLASS_CONFLICT')
    if (request.shares <= 0 or request.fill_price <= 0 or request.reference_mark is not None and request.reference_mark <= 0
            or any(v <= 0 for v in request.other_prices.values())):
        raise WhatIfUnavailable('POSITIVE_SHARES_AND_PRICES_REQUIRED')
    positions = [p for p in snapshot.positions if p.account_id == request.account_id]
    quantities = {p.symbol: p.quantity for p in positions}
    allowed_marks = set(quantities) - {request.symbol}
    if set(request.other_prices) - allowed_marks:
        raise WhatIfUnavailable('MARKS_MUST_MATCH_OTHER_ACCOUNT_HOLDINGS')
    account = next(a for a in snapshot.accounts if a.account_id == request.account_id)
    literals = [account.cash, request.shares, request.fill_price, request.fee,
                *request.other_prices.values(), *(v for p in positions for v in (p.quantity, p.avg_cost))]
    if request.reference_mark is not None:
        literals.append(request.reference_mark)
    with localcontext() as ctx:
        ctx.prec = max(80, sum(len(v.as_tuple().digits) + abs(v.as_tuple().exponent) for v in literals) + 40)
        # Recompute inside the protected context, including original cost products.
        costs = {p.symbol: p.quantity * p.avg_cost for p in positions}
        before_quantity = quantities.get(request.symbol, Decimal(0))
        delta = request.shares if request.side == 'BUY' else -request.shares
        if before_quantity + delta < 0:
            raise WhatIfUnavailable('OVERSELL_NOT_ALLOWED')
        gross = request.shares * request.fill_price
        after_cash = account.cash - gross - request.fee if delta > 0 else account.cash + gross - request.fee
        if after_cash < 0:
            raise WhatIfUnavailable('INSUFFICIENT_CASH_INCLUDING_FEES')
        prices = {**request.other_prices, request.symbol: request.fill_price}
        def state(qty, cash, marks):
            missing = sorted(s for s, q in qty.items() if q and s not in marks)
            values = {s: q * marks[s] for s, q in qty.items() if q and s in marks}
            equity = sum(values.values(), Decimal(0)) if not missing else None
            nav = cash + equity if equity is not None else None
            weights = {s: v / nav for s, v in values.items()} if nav else {}
            focus_value = qty.get(request.symbol, Decimal(0)) * marks[request.symbol]
            return {'shares': plain(qty.get(request.symbol, Decimal(0))), 'cash': plain(cash),
                'focus_market_value': plain(focus_value),
                'equity': plain(equity) if equity is not None else None,
                'nav': plain(nav) if nav is not None else None,
                'focus_weight': plain(focus_value / nav) if nav else None,
                'largest_weight': plain(max(weights.values(), default=Decimal(0))) if nav else None,
                'hhi_nav': plain(sum((w*w for w in weights.values()), Decimal(0))) if nav else None,
                'missing_price_symbols': missing}
        before = state(quantities, account.cash, prices)
        after_quantities = {**quantities, request.symbol: before_quantity + delta}
        after = state(after_quantities, after_cash, prices)
        baseline = state(quantities, account.cash, {**prices, request.symbol: request.reference_mark}) if request.reference_mark is not None else None
        repricing = before_quantity * (request.fill_price-request.reference_mark) if request.reference_mark is not None else None
        original_cost = costs.get(request.symbol, Decimal(0))
        average = original_cost / before_quantity if before_quantity else Decimal(0)
        after_cost = original_cost + gross + request.fee if delta > 0 else original_cost - average * request.shares
        realized = None if delta > 0 else gross - average * request.shares - request.fee
        receipt = {'version': 2, 'task_id': task.task_id, 'revision': task.revision,
            'bound_task_hash': content_hash(task.model_dump(mode='json')),
            'snapshot_id': snapshot.snapshot_id, 'snapshot_hash': snapshot.content_hash,
            'snapshot_as_of': snapshot.as_of.isoformat(), 'research_cutoff': task.cutoff.isoformat(),
            'input': request.model_dump(mode='json'), 'before': before, 'after': after,
            'baseline_before': baseline,
            'market_repricing_delta': plain(repricing) if repricing is not None else None,
            'trade_nav_delta': plain(-request.fee),
            'total_nav_delta': plain(repricing-request.fee) if repricing is not None else None,
            'repricing_identity': 'USER_ASSERTION_HYPOTHETICAL_REFERENCE_NOT_ACTUAL_MARKET_MOVE',
            'gross_value': plain(gross), 'cash_delta': plain(after_cash-account.cash),
            'focus_cost_before': plain(original_cost), 'focus_cost_after': plain(after_cost),
            'modeled_realized_pnl': plain(realized) if realized is not None else None,
            'modeled_unrealized_pnl_after': plain((before_quantity+delta)*request.fill_price-after_cost),
            'identity': 'USER_ASSERTION', 'price_basis': 'HYPOTHETICAL_MARKS_SAME_BEFORE_AND_AFTER',
            'actual_quote': None, 'actual_fill': False, 'orders_created': 0, 'persisted': False,
            'original_action': task.action_decision.decision_action,
            'limitations': ['USD cash account only; no leverage, shorts, tax lots, FX or execution.',
                'Hypothetical fill marks the focus position before and after; other marks are user assumptions, not market data.',
                'Missing marks disable NAV/weights/concentration separately for each state; zero NAV has undefined weights.',
                'Cost/P&L use average cost with buy fees capitalized and sell fees deducted; not tax accounting.',
                'Concentration is single-name NAV weight and sum of squared equity/NAV weights, excluding cash; not full risk exposure.',
                'Share-basis continuity is user-attested, not independently verified; saved Action is unchanged.']}
        return {**receipt, 'content_hash': content_hash(receipt)}
