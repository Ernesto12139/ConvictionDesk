"""Portfolio-level What-if: rehearse one account change without a saved Action.

Reuses portfolio_whatif.simulate unchanged (Decimal, single account, no orders,
no writes) against the current portfolio snapshot. Suggested prices come from
the live quote reference and are pre-filled only as editable assumptions; the
receipt still records every mark as USER_ASSERTION. A missing quote leaves the
field empty, never zero.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import HTTPException, Query

from .portfolio_whatif import WhatIfRequest, WhatIfUnavailable, plain, simulate
from .provider import SYMBOL


class _Preview:
    """The minimal task surface simulate() reads; bound to the snapshot only."""

    def __init__(self, snapshot, symbol, now):
        self.task_id = 'PORTFOLIO_PREVIEW'
        self.revision = 1
        self.status = 'COMPLETE'
        self.focus_symbol = symbol
        self.account_id = None
        self.snapshot_id = snapshot.snapshot_id
        self.snapshot_hash = snapshot.content_hash
        self.cutoff = now
        self.sources = []
        self.action_decision = SimpleNamespace(decision_action='NO_SAVED_ACTION')

    def model_dump(self, mode='json'):
        return {'task_id': self.task_id, 'focus_symbol': self.focus_symbol, 'snapshot_id': self.snapshot_id,
                'snapshot_hash': self.snapshot_hash, 'cutoff': self.cutoff.isoformat()}


def _snapshot(store, now):
    snapshot = store.get_snapshot_known_at(now)
    if snapshot is None:
        raise HTTPException(422, 'PORTFOLIO_SNAPSHOT_REQUIRED')
    return snapshot


def register_portfolio_preview(app, store, pulse, clock):
    def now():
        return clock().astimezone(timezone.utc)

    @app.get('/api/portfolio/what-if/context')
    def context(symbol: str = Query(min_length=1, max_length=15)):
        symbol = symbol.upper()
        if not SYMBOL.fullmatch(symbol):
            raise HTTPException(422, 'INVALID_SYMBOL')
        moment = now()
        snapshot = _snapshot(store, moment)
        held = sorted({p.symbol for p in snapshot.positions} | {symbol})
        quotes = {s: pulse.quote(s, moment) if pulse.status == 'AVAILABLE' else {'status': pulse.status}
                  for s in held}
        suggested = {s: q['price'] for s, q in quotes.items() if q.get('status') == 'AVAILABLE'}
        return {'symbol': symbol, 'snapshot_id': snapshot.snapshot_id, 'snapshot_as_of': snapshot.as_of.isoformat(),
                'scenario_date': moment.date().isoformat(),
                'accounts': [{'account_id': a.account_id, 'name': a.name, 'cash': plain(a.cash),
                              'positions': [{'symbol': p.symbol, 'shares': plain(p.quantity)}
                                            for p in snapshot.positions if p.account_id == a.account_id]}
                             for a in snapshot.accounts],
                'suggested_prices': suggested,
                'quote_basis': {s: {k: q.get(k) for k in ('status', 'as_of', 'provider', 'basis')}
                                for s, q in quotes.items()},
                'price_meaning': ('Latest provider quote reference, possibly delayed, pre-filled as an '
                                  'editable assumption. Not a fill, not an order.')}

    @app.post('/api/portfolio/what-if')
    def preview(request: WhatIfRequest):
        moment = now()
        snapshot = _snapshot(store, moment)
        try:
            receipt = simulate(_Preview(snapshot, request.symbol, moment), snapshot, request)
        except WhatIfUnavailable as error:
            raise HTTPException(422, str(error)) from None
        return receipt | {'binding': 'CURRENT_PORTFOLIO_SNAPSHOT', 'computed_at': moment.isoformat()}
