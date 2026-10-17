"""Deterministic, explicitly hypothetical research context; no trades or writes."""
from decimal import Decimal, localcontext

from .analytics import compute_portfolio_overview


def research_portfolio_context(snapshot, scoped, observations, symbol, user_price, account_id):
    quantities = [p.quantity for p in scoped.positions if p.symbol == symbol]
    # Preserve complete accepted literals even beyond ambient Decimal precision.
    values = [v for p in scoped.positions for v in (p.quantity, p.avg_cost)]
    values += [a.cash for a in scoped.accounts] + [o.price for o in observations]
    if user_price is not None:
        values.append(user_price)
    with localcontext() as ctx:
        ctx.prec = max(28, 2 * max((len(v.as_tuple().digits) for v in values), default=1)
                       + max((v.adjusted() for v in values), default=0)
                       - min((v.as_tuple().exponent for v in values), default=0) + len(str(len(values))) + 4)
        held = {p.symbol for p in scoped.positions}
        metrics = compute_portfolio_overview(scoped, [o for o in observations if o.symbol in held])
        row = next((p.model_dump(mode='json') for p in metrics.positions if p.symbol == symbol), None)
        reference = next((o for o in observations if o.symbol == symbol), None)
        scenario = None
        if user_price is not None and row:
            value = sum(quantities, Decimal(0)) * user_price
            cost = Decimal(row['cost_basis'])
            pnl = value - cost
            # Only the focus position changes price; other holdings retain their
            # qualified reference observations. Missing prices stay unknown.
            other_equity = sum((p.market_value for p in metrics.positions
                                if p.symbol != symbol and p.market_value is not None), Decimal(0))
            missing = sorted(set(metrics.missing_price_symbols) - {symbol})
            nav = value + other_equity + metrics.cash if not missing else None
            ratio = value / nav if nav else None
            pnl_ratio = pnl / cost if cost else None
            scenario = {'basis': 'USER_ASSERTION_HYPOTHETICAL', 'price_verified': False,
                'price': str(user_price), 'market_value': str(value), 'unrealized_pnl': str(pnl),
                'unrealized_pnl_percent': str(pnl_ratio) if pnl_ratio is not None else None,
                'unrealized_pnl_percent_display': f'{pnl_ratio * 100:.2f}%' if pnl_ratio is not None else None,
                'nav': str(nav) if nav is not None else None,
                'nav_weight': str(ratio) if ratio is not None else None,
                'nav_weight_percent_display': f'{ratio * 100:.2f}%' if ratio is not None else None,
                'missing_price_symbols': missing,
                'unknown_reason': 'OTHER_HOLDING_PRICES_MISSING' if missing else None,
                'price_delta': str(user_price - reference.price) if reference else None,
                'price_gap': str(abs(user_price - reference.price)) if reference else None,
                'price_delta_percent_display': f'{(user_price / reference.price - 1) * 100:.2f}%' if reference else None,
                'price_gap_percent_display': f'{abs(user_price / reference.price - 1) * 100:.2f}%' if reference else None,
                'reference_evidence_id': reference.evidence_id if reference else None,
                'assumptions': 'Only focus price changes; other qualified closes and snapshot cash remain fixed; no trade, fees, taxes or FX.'}
        nav = metrics.priced_equity + metrics.cash if metrics.valuation_status == 'complete' else None
        return {'snapshot_id': snapshot.snapshot_id, 'snapshot_hash': snapshot.content_hash,
            'portfolio_id': snapshot.portfolio_id, 'account_id': account_id,
            'account_names': [a.name for a in scoped.accounts],
            'basis': 'COMPLETED_CLOSE', 'valuation_status': metrics.valuation_status,
            'missing_price_symbols': metrics.missing_price_symbols, 'cash': str(metrics.cash), 'position': row,
            'total_cost': str(metrics.total_cost), 'nav': str(nav) if nav is not None else None,
            'user_price_value': scenario['market_value'] if scenario else None,
            'user_price_basis': 'USER_ASSERTION_HYPOTHETICAL', 'scenario': scenario,
            'numeric_sizing_policy': 'NOT_CONFIRMED',
            'market_evidence_ids': [o.evidence_id for o in observations]}
