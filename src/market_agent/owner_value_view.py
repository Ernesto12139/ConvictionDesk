"""Read-only presentation adapter for the Owner holding value slice.

The unified UI (issue #29) must not import the analysis models directly, and it
must never receive private account fields it did not ask for. This module maps an
already-built ``OwnerValueSlice`` to a typed, framework-free view model.

It performs no I/O: no database, network, provider, model, order or persistence.
Every amount is exact decimal text; no float is introduced at the boundary.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .domain import FrozenModel
from .owner_value_slice import OwnerValueSlice, plain

VIEW_SCHEMA = 'owner-value-ui-v1'


class UiBadge(FrozenModel):
    code: str
    tone: Literal['positive', 'neutral', 'warning', 'critical', 'unknown']
    label: str


class UiHeadline(FrozenModel):
    status: Literal['ATTENTION', 'REVIEW_NEEDED', 'GAP_DOCUMENTED', 'NORMAL', 'UNKNOWN']
    title: str
    detail: str


class UiHoldingCard(FrozenModel):
    symbol: str
    account_ref: str
    currency: str
    as_of: str
    freshness: Literal['CURRENT', 'STALE']
    quantity: str | None = None
    avg_cost: str | None = None


class UiValuationCard(FrozenModel):
    status: Literal['VALUED', 'UNKNOWN']
    value_text: str | None = None
    price_text: str | None = None
    price_as_of: str | None = None
    current: bool
    reasons: tuple[str, ...] = ()


class UiEventCard(FrozenModel):
    form: str | None
    items: tuple[str, ...] = ()
    accepted_at: str | None = None
    accession: str | None = None
    body_status: str
    item_section: str | None = None
    officer_summary: str | None = None
    excerpt: str | None = None
    source_uri: str | None = None


class UiThesisCard(FrozenModel):
    status: str
    thesis_as_of: str | None = None
    owner_reviewed: bool
    relevance: str
    thesis_anchors: tuple[str, ...] = ()
    anchor_hits_in_body: tuple[str, ...] = ()
    matched_item_count: int = Field(ge=0)
    next_check: str | None = None
    next_check_status: str


class UiWhatIfCard(FrozenModel):
    mode: str
    side: str
    shares: str
    scenario_price: str
    orders_created: int
    nav_basis: str
    gross_value: str | None = None
    cash_before: str | None = None
    cash_after: str | None = None
    nav_text: str | None = None


class OwnerValueView(FrozenModel):
    schema_version: Literal['owner-value-ui-v1'] = VIEW_SCHEMA
    generated_at: str
    symbol: str
    redaction: Literal['LOCAL_FULL', 'SHAREABLE_REDACTED']
    headline: UiHeadline
    holding: UiHoldingCard
    valuation: UiValuationCard
    event: UiEventCard
    thesis: UiThesisCard
    whatif: UiWhatIfCard | None = None
    badges: tuple[UiBadge, ...] = ()
    limitations: tuple[str, ...] = ()


def _officer_summary(slice: OwnerValueSlice) -> str | None:
    officer = slice.officer_change
    if officer is None or officer.status == 'UNPARSED':
        return None
    parts = []
    if officer.departing_name:
        parts.append(f'departing {officer.departing_name}'
                     + (f' as {officer.departing_role}' if officer.departing_role else ''))
    if officer.successor_name:
        parts.append(f'successor {officer.successor_name}'
                     + (f' as {officer.successor_role}' if officer.successor_role else ''))
    if officer.effective_date:
        parts.append(f'effective {officer.effective_date}')
    return '; '.join(parts) or None


def _headline(slice: OwnerValueSlice) -> UiHeadline:
    if slice.valuation.status == 'UNKNOWN':
        return UiHeadline(
            status='ATTENTION',
            title='Value unavailable',
            detail='No qualified price: ' + ', '.join(slice.valuation.reasons))
    if slice.filing_body.status != 'BODY_VERIFIED':
        return UiHeadline(
            status='REVIEW_NEEDED',
            title='Filing body required',
            detail='The event is eligible but its body is not captured; impact is undetermined.')
    if slice.thesis_check.relevance == 'RELATED_TO_THESIS':
        return UiHeadline(
            status='REVIEW_NEEDED',
            title='Thesis anchor mentioned',
            detail='The filing body mentions a thesis anchor; verify direction and magnitude.')
    if slice.thesis_check.next_check_status == 'NO_THESIS_ANCHOR_IN_BODY':
        return UiHeadline(
            status='GAP_DOCUMENTED',
            title='No thesis anchor in event body',
            detail='The event body does not address the prior thesis anchors.')
    return UiHeadline(status='NORMAL', title='No review trigger', detail='No gap was detected.')


def _badges(slice: OwnerValueSlice) -> tuple[UiBadge, ...]:
    price_tone = ('positive' if slice.price.status == 'QUALIFIED'
                  else 'warning' if slice.price.status == 'STALE' else 'critical')
    badges = [
        UiBadge(code=f'PRICE_{slice.price.status}', tone=price_tone,
                label=f'Price {slice.price.status.lower()}'),
        UiBadge(code=slice.filing_body.status,
                tone='positive' if slice.filing_body.status == 'BODY_VERIFIED' else 'warning',
                label='Filing body captured' if slice.filing_body.status == 'BODY_VERIFIED'
                else 'Filing body missing'),
    ]
    if slice.instrument.status == 'VERIFIED_FROM_FILING':
        badges.append(UiBadge(
            code='INSTRUMENT_VERIFIED', tone='positive',
            label=f'{slice.instrument.security_title} on {slice.instrument.exchange}'))
    elif slice.instrument.status == 'INSTRUMENT_SYMBOL_MISMATCH':
        badges.append(UiBadge(code='INSTRUMENT_MISMATCH', tone='critical',
                              label='Filing security identity mismatch'))
    if not slice.thesis_check.owner_reviewed:
        badges.append(UiBadge(code='AI_DRAFT_THESIS', tone='warning',
                              label='Prior thesis is an AI draft'))
    return tuple(badges)


def to_ui_view(slice: OwnerValueSlice) -> OwnerValueView:
    local = slice.redaction == 'LOCAL_FULL'
    valuation = slice.valuation
    holding = slice.holding
    event = slice.event
    thesis = slice.thesis_check
    whatif = slice.whatif
    return OwnerValueView(
        generated_at=slice.generated_at.isoformat(),
        symbol=holding.symbol,
        redaction=slice.redaction,
        headline=_headline(slice),
        holding=UiHoldingCard(
            symbol=holding.symbol,
            account_ref=holding.account_label if local else 'REDACTED',
            currency=holding.currency,
            as_of=holding.as_of.isoformat(),
            freshness=slice.holding_scope.holding_freshness,
            quantity=plain(holding.quantity) if local else None,
            avg_cost=plain(holding.avg_cost) if local else None),
        valuation=UiValuationCard(
            status=valuation.status,
            value_text=(plain(valuation.market_value)
                        if valuation.market_value is not None else None),
            price_text=(plain(valuation.price)
                        if valuation.price is not None else None),
            price_as_of=(valuation.price_as_of.isoformat()
                         if valuation.price_as_of is not None else None),
            current=valuation.current,
            reasons=valuation.reasons),
        event=UiEventCard(
            form=event.form, items=event.items,
            accepted_at=(event.accepted_at.isoformat()
                         if event.accepted_at is not None else None),
            accession=event.accession,
            body_status=slice.filing_body.status,
            item_section=slice.filing_body.item_section,
            officer_summary=_officer_summary(slice),
            excerpt=(slice.officer_change.excerpt
                     if slice.officer_change is not None else None),
            source_uri=event.source_uri),
        thesis=UiThesisCard(
            status=thesis.status,
            thesis_as_of=(thesis.thesis_as_of.isoformat()
                          if thesis.thesis_as_of is not None else None),
            owner_reviewed=thesis.owner_reviewed,
            relevance=thesis.relevance,
            thesis_anchors=thesis.thesis_anchors,
            anchor_hits_in_body=thesis.anchor_hits_in_body,
            matched_item_count=len(thesis.matched_items),
            next_check=thesis.next_check,
            next_check_status=thesis.next_check_status),
        whatif=(UiWhatIfCard(
            mode=whatif.mode, side=whatif.side, shares=plain(whatif.shares),
            scenario_price=plain(whatif.scenario_price),
            orders_created=whatif.orders_created, nav_basis=whatif.nav_basis,
            gross_value=plain(whatif.gross_value) if local else None,
            cash_before=plain(whatif.cash_before) if local else None,
            cash_after=plain(whatif.cash_after) if local else None,
            nav_text=(plain(whatif.nav_after)
                      if local and whatif.nav_after is not None else None))
            if whatif is not None else None),
        badges=_badges(slice),
        limitations=(
            'Presentation only: no order, persistence, provider call or external send.',
            'A documented thesis gap is not an investment recommendation.',
        ),
    )
