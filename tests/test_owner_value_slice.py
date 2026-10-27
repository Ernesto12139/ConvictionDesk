"""Source-bound tests for the read-only Owner holding value slice.

Every fixture is synthetic or public SEC metadata. No Owner database, provider,
network, model, order or persistence is used.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

import market_agent.owner_value_slice as module
from market_agent.domain import (PortfolioContent, PortfolioSnapshot,
                                 portfolio_content_hash)
from market_agent.owner_value_slice import (PriceObservationInput, SecEvent,
                                            SecFilingBody, SliceRefused, ThesisItem,
                                            ThesisRecord, WhatIfRequest,
                                            build_owner_value_slice)
from market_agent.owner_value_view import to_ui_view

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'evals' / 'fixtures' / 'owner_value_003'
EVAL = datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


def snapshot():
    return PortfolioSnapshot.model_validate(load('snapshot_primary.json'))


def content_from_fixture(**overrides):
    data = load('snapshot_primary.json')
    data.pop('snapshot_id', None)
    data.pop('content_hash', None)
    data.update(overrides)
    return data


def rebuild(**overrides):
    data = content_from_fixture(**overrides)
    parsed = PortfolioContent.model_validate(data)
    return PortfolioSnapshot(**data, snapshot_id='fixture-snapshot-rebuilt',
                             content_hash=portfolio_content_hash(parsed))


def event(**overrides):
    data = load('sec_event_8k_5_02.json')
    data.update(overrides)
    return SecEvent.model_validate(data)


def thesis(**overrides):
    data = load('thesis_ai_draft.json')
    data.update(overrides)
    return ThesisRecord.model_validate(data)


def stale_price(**overrides):
    data = load('price_stale_massive.json')
    data.update(overrides)
    return PriceObservationInput.model_validate(data)


def fresh_price(**overrides):
    data = load('price_fresh_qualified.json')
    data.update(overrides)
    return PriceObservationInput.model_validate(data)


def mark(symbol, price, **overrides):
    """A synthetic peer mark; only a fully qualified observation may enter NAV."""
    data = {'symbol': symbol, 'price': price, 'currency': 'USD',
            'provider': 'fixture-peer-mark', 'observed_at': '2026-10-26T20:00:00Z',
            'retrieved_at': '2026-10-26T20:00:05Z', 'price_basis': 'UNADJUSTED',
            'usage_rights': 'OWNER_AUTHORIZED_LOCAL_ONLY',
            'source_uri': f'https://fixture.invalid/quote/{symbol}',
            'evidence_id': f'EFIXTUREMARK{symbol}'}
    data.update(overrides)
    return PriceObservationInput.model_validate(data)


def body(**overrides):
    data = load('sec_body_8k_5_02.json')
    data.update(overrides)
    return SecFilingBody.model_validate(data)


def whatif(**overrides):
    data = {'side': 'BUY', 'shares': '5', 'price_basis': 'HYPOTHETICAL_OPERATOR_INPUT',
            'price': '25.00', 'fee': '0',
            'rationale': 'Illustrative sizing only; not an order.'}
    data.update(overrides)
    return WhatIfRequest.model_validate(data)


def build(**overrides):
    args = dict(snapshot=snapshot(), account_id='a0', symbol='GLXY',
                evaluation_time=EVAL, event=event(), filing_body=body(),
                price=stale_price(), thesis=thesis(), whatif=whatif())
    args.update(overrides)
    return build_owner_value_slice(**args)


def test_candidate_source_identity():
    assert module.SLICE_SCHEMA == 'owner-value-slice-v1'
    assert Path(module.__file__).resolve().parent == ROOT / 'src' / 'market_agent'


def test_one_holding_event_thesis_and_hypothetical_whatif():
    result = build()
    assert result.event.status == 'ELIGIBLE'
    assert result.event.accession == '0001859392-26-000105'
    assert result.event.accepted_at < result.event.captured_at
    assert result.price.status == 'STALE'
    assert result.price.valuation_eligible is False
    assert result.valuation.status == 'UNKNOWN'
    assert result.valuation.market_value is None
    assert 'PRICE_STALE' in result.valuation.reasons
    assert result.thesis_check.status == 'THESIS_AVAILABLE'
    assert result.thesis_check.thesis_status == 'AI_DRAFT'
    assert result.thesis_check.owner_reviewed is False
    assert result.thesis_check.event_topics == ('OFFICER_OR_DIRECTOR_CHANGE',)
    receipt = result.whatif
    assert receipt.mode == 'HYPOTHETICAL_SCENARIO'
    assert receipt.cash_before == Decimal('1000')
    assert receipt.cash_after == Decimal('875')
    assert receipt.focus_value_before == Decimal('175.00')
    assert receipt.focus_value_after == Decimal('300.00')
    assert receipt.nav_basis == 'UNKNOWN'
    assert receipt.nav_before is None
    assert receipt.focus_weight_after is None
    assert receipt.unpriced_position_count == 3
    assert receipt.orders_created == 0
    assert receipt.persisted is False
    assert receipt.external_send is False


def test_captured_filing_body_grounds_a_gap_not_a_claim():
    result = build()
    assert result.filing_body.status == 'BODY_VERIFIED'
    assert result.filing_body.document_id == 'SDa7082bd82c9b0c78'
    assert result.filing_body.item_section.startswith('Item 5.02')
    officer = result.officer_change
    assert officer.status == 'PARSED'
    assert officer.departing_name == 'Robert Rico'
    assert officer.departing_role == 'Chief Accounting Officer'
    assert officer.successor_name == 'Dritan Muneka'
    assert officer.effective_date == '2026-09-30'
    assert officer.notice_date == '2026-08-05'
    assert officer.appointment_date == '2026-09-22'
    for value in (officer.departing_name, officer.departing_role,
                  officer.successor_name):
        assert value in officer.excerpt
    # The role combines the filed title with the separate officer title.
    for value in ('principal accounting officer', 'Corporate Controller'):
        assert value in officer.excerpt
    assert officer.excerpt.endswith('.')
    assert result.thesis_check.relevance == 'NOT_ADDRESSED'
    assert result.thesis_check.thesis_anchors == ('SHARE_REPURCHASE', 'TOKENIZATION')
    assert result.thesis_check.anchor_hits_in_body == ()
    assert result.thesis_check.matched_items == ()
    assert result.thesis_check.next_check_status == 'NO_THESIS_ANCHOR_IN_BODY'
    assert 'does not mention' in result.thesis_check.next_check
    assert 'NO_THESIS_IMPACT' in result.thesis_check.next_check


def test_instrument_identity_comes_from_the_filing_itself():
    instrument = build().instrument
    assert instrument.status == 'VERIFIED_FROM_FILING'
    assert instrument.symbol == 'GLXY'
    assert instrument.issuer_name == 'Galaxy Digital Inc.'
    assert instrument.security_title == 'Class A Common Stock'
    assert instrument.share_class == 'CLASS_A'
    assert instrument.par_value == Decimal('0.001')
    assert instrument.exchange == 'The Nasdaq Stock Market LLC'
    assert instrument.cik == '0001859392'


def test_filing_security_symbol_mismatch_blocks_valuation():
    shifted = body(chunks=[
        {**chunk, 'content_hash': module.content_hash({
            'document_id': 'SDa7082bd82c9b0c78',
            'sequence': chunk['sequence'],
            'section': chunk['section'],
            'text': chunk['text'].replace('GLXY The Nasdaq', 'GLXYQ The Nasdaq'),
        }), 'text': chunk['text'].replace('GLXY The Nasdaq', 'GLXYQ The Nasdaq')}
        if 'GLXY The Nasdaq' in chunk['text'] else chunk
        for chunk in load('sec_body_8k_5_02.json')['chunks']])
    result = build(filing_body=shifted, price=fresh_price(),
                   whatif=None)
    assert result.instrument.status == 'INSTRUMENT_SYMBOL_MISMATCH'
    assert result.valuation.status == 'UNKNOWN'
    assert 'INSTRUMENT_IDENTITY_MISMATCH' in result.valuation.reasons


def test_content_hash_is_recomputable():
    result = build()
    payload = result.model_dump(mode='json', exclude={'content_hash'})
    assert result.content_hash == module.content_hash(payload)


def test_account_isolation_two_accounts_hold_same_symbol():
    first = build(account_id='a0', whatif=None)
    second = build(account_id='a1', whatif=None)
    assert first.holding.quantity == Decimal('7')
    assert second.holding.quantity == Decimal('23')
    assert first.account_scope.same_symbol_other_accounts == ('a1',)
    assert second.account_scope.same_symbol_other_accounts == ('a0',)
    assert first.holding.quantity != Decimal('30')


def test_unknown_account_or_unheld_symbol_is_refused():
    with pytest.raises(SliceRefused, match='ACCOUNT_NOT_FOUND'):
        build(account_id='missing')
    with pytest.raises(SliceRefused, match='SYMBOL_NOT_HELD_IN_ACCOUNT'):
        build(symbol='MSFT')


def test_snapshot_after_evaluation_is_refused():
    with pytest.raises(SliceRefused, match='SNAPSHOT_AFTER_EVALUATION'):
        build(evaluation_time=datetime(2026, 10, 20, tzinfo=timezone.utc))


def test_forged_snapshot_hash_is_rejected_by_contract():
    data = load('snapshot_primary.json')
    data['content_hash'] = 'sha256:' + '0' * 64
    with pytest.raises(ValidationError, match='content_hash'):
        PortfolioSnapshot.model_validate(data)


def test_price_wrong_symbol_is_not_used():
    result = build(price=stale_price(symbol='AAPL'))
    assert result.price.status == 'SYMBOL_MISMATCH'
    assert result.valuation.status == 'UNKNOWN'


def test_price_currency_mismatch_has_no_fx():
    result = build(price=stale_price(currency='EUR'))
    assert result.price.status == 'CURRENCY_MISMATCH'
    assert 'NO_FX_CONVERSION_AUTHORIZED' in result.price.reasons
    assert result.valuation.market_value is None


def test_price_observed_or_retrieved_after_evaluation_is_refused():
    late = EVAL + timedelta(days=1)
    assert build(price=stale_price(observed_at=late)).price.status == 'PRICE_AFTER_EVALUATION'
    assert build(price=stale_price(retrieved_at=late)).price.status == 'RETRIEVED_AFTER_EVALUATION'


def test_retrieval_cannot_precede_observation():
    result = build(price=stale_price(retrieved_at='2026-09-30T00:00:00Z'))
    assert result.price.status == 'RETRIEVED_BEFORE_OBSERVED'


def test_fresh_price_without_rights_is_blocked():
    result = build(price=fresh_price(usage_rights='DISPLAY_ONLY'))
    assert result.price.status == 'RIGHTS_BLOCKED'
    assert result.valuation.status == 'UNKNOWN'
    unknown = build(price=fresh_price(usage_rights='UNKNOWN'))
    assert unknown.price.status == 'RIGHTS_BLOCKED'


def test_price_basis_must_be_unadjusted():
    assert build(price=fresh_price(price_basis='ADJUSTED')).price.status == 'PRICE_BASIS_UNKNOWN'
    assert build(price=fresh_price(price_basis='UNKNOWN')).price.status == 'PRICE_BASIS_UNKNOWN'


def test_stale_price_is_never_a_real_valuation():
    result = build(price=stale_price())
    assert result.price.status == 'STALE'
    assert result.price.age_seconds > 3 * 86400
    assert result.valuation.status == 'UNKNOWN'
    assert result.valuation.basis == 'NONE'


def test_qualified_price_values_as_of_holding_and_computes_nav():
    peers = (mark('AAPL', '200'), mark('KLAC', '170'))
    result = build(price=fresh_price(), other_prices=peers)
    assert result.price.status == 'QUALIFIED'
    assert result.valuation.status == 'VALUED'
    assert result.valuation.market_value == Decimal('175.70')
    assert result.account_scope.priced_position_count == 3
    receipt = result.whatif
    assert receipt.mode == 'HYPOTHETICAL_SCENARIO'
    assert receipt.nav_basis == 'QUALIFIED_MARKS'
    # NAV uses the qualified GLXY mark, not the operator's hypothetical price.
    assert receipt.nav_before == Decimal('2115.70')
    assert receipt.nav_after == Decimal('2116.20')
    assert receipt.qualified_mark_count == 3
    assert receipt.unqualified_mark_count == 0
    expected = Decimal('175.70') / Decimal('2115.70')
    assert abs(receipt.focus_weight_before - expected) < Decimal('1e-20')


def test_qualified_market_whatif_requires_qualified_price():
    with pytest.raises(SliceRefused, match='QUALIFIED_MARKET_PRICE_REQUIRED'):
        build(whatif=whatif(price_basis='QUALIFIED_MARKET', price='22.74'))
    result = build(price=fresh_price(),
                   whatif=whatif(price_basis='QUALIFIED_MARKET', price='25.10'))
    assert result.whatif.mode == 'QUALIFIED_MARKET_SCENARIO'


def test_float_and_inexact_numbers_are_rejected():
    for bad in (25.1, True, '2.51e1', 'NaN', 'Infinity'):
        with pytest.raises(ValidationError):
            PriceObservationInput.model_validate({**load('price_fresh_qualified.json'),
                                                  'price': bad})
    for bad in (5.0, '5e0', True):
        with pytest.raises(ValidationError):
            whatif(shares=bad)
    with pytest.raises(ValidationError):
        whatif(price='0')


def test_whatif_refuses_oversell_and_insufficient_cash():
    with pytest.raises(SliceRefused, match='OVERSELL_NOT_ALLOWED'):
        build(whatif=whatif(side='SELL', shares='8', price='20'))
    with pytest.raises(SliceRefused, match='INSUFFICIENT_CASH_INCLUDING_FEES'):
        build(whatif=whatif(side='BUY', shares='41', price='25'))


def test_missing_thesis_is_explicit_not_silent():
    result = build(thesis=None)
    assert result.thesis_check.status == 'THESIS_NOT_AVAILABLE'
    assert result.thesis_check.next_check_status == 'NO_PRIOR_THESIS'
    assert result.thesis_check.relevance == 'UNDETERMINED'
    assert result.event.status == 'ELIGIBLE'


def test_without_body_the_next_check_still_requires_the_document_body():
    result = build(filing_body=None)
    assert result.filing_body.status == 'BODY_NOT_PROVIDED'
    assert result.instrument.status == 'UNVERIFIED'
    assert result.thesis_check.relevance == 'UNDETERMINED'
    assert result.thesis_check.next_check_status == 'REQUIRES_DOCUMENT_BODY'
    assert 'filing body' in result.thesis_check.next_check.lower()


def test_unverified_instrument_does_not_by_itself_block_a_qualified_price():
    result = build(filing_body=None, price=fresh_price(), whatif=None)
    assert result.instrument.status == 'UNVERIFIED'
    assert result.valuation.status == 'VALUED'


def test_thesis_after_event_is_refused():
    result = build(thesis=thesis(as_of=datetime(2026, 10, 1, tzinfo=timezone.utc)))
    assert result.thesis_check.status == 'THESIS_AFTER_EVENT'
    assert 'THESIS_MUST_STRICTLY_PREDATE_EVENT_AND_EVALUATION' in result.thesis_check.reasons


def test_ungrounded_thesis_is_refused():
    data = load('thesis_ai_draft.json')
    data['core_drivers'] = [{'text': 'Uncited claim', 'evidence_ids': []}]
    result = build(thesis=ThesisRecord.model_validate(data))
    assert result.thesis_check.status == 'THESIS_UNGROUNDED'
    empty = load('thesis_ai_draft.json')
    for key in ('core_drivers', 'key_assumptions', 'execution_risks',
                'invalidation_conditions'):
        empty[key] = []
    result = build(thesis=ThesisRecord.model_validate(empty))
    assert result.thesis_check.status == 'THESIS_UNGROUNDED'


def test_thesis_wrong_symbol_is_refused():
    result = build(thesis=thesis(symbol='AAPL'))
    assert result.thesis_check.status == 'THESIS_SYMBOL_MISMATCH'


def test_event_pit_boundaries_are_distinct():
    accepted_late = event(accepted_at=EVAL + timedelta(hours=1))
    assert build(event=accepted_late).event.status == \
        'PIT_INELIGIBLE_NOT_ACCEPTED_BY_EVALUATION'
    captured_late = event(captured_at=EVAL + timedelta(hours=1))
    assert build(event=captured_late).event.status == 'EVIDENCE_NOT_CAPTURED_BY_EVALUATION'
    backwards = event(captured_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert build(event=backwards).event.status == 'CAPTURE_PRECEDES_ACCEPTANCE'


def test_event_form_items_rights_and_symbol_negatives():
    assert build(event=event(form='10-Q')).event.status == 'EVENT_FORM_NOT_ELIGIBLE'
    assert build(event=event(items=())).event.status == 'EVENT_ITEMS_EMPTY'
    assert build(event=event(rights='PROPRIETARY')).event.status == \
        'RIGHTS_NOT_PUBLIC_FILING_METADATA'
    assert build(event=event(symbol='AAPL')).event.status == 'EVENT_SYMBOL_MISMATCH'
    assert build(event=None).event.status == 'EVENT_NOT_PROVIDED'


DOC_ID = 'SDa7082bd82c9b0c78'


def rewrite_body(text_by_sequence):
    data = load('sec_body_8k_5_02.json')
    chunks = []
    for chunk in data['chunks']:
        text = text_by_sequence.get(chunk['sequence'], chunk['text'])
        chunks.append({**chunk, 'text': text,
                       'content_hash': module.content_hash({
                           'document_id': DOC_ID, 'sequence': chunk['sequence'],
                           'section': chunk['section'], 'text': text})})
    data['chunks'] = chunks
    return SecFilingBody.model_validate(data)


def test_body_identity_and_pit_negatives():
    assert build(filing_body=body(accession='0001859392-26-000104')
                 ).filing_body.status == 'BODY_ACCESSION_MISMATCH'
    assert build(filing_body=body(cik='0000000001')
                 ).filing_body.status == 'BODY_ISSUER_MISMATCH'
    assert build(filing_body=body(form='10-Q')
                 ).filing_body.status == 'BODY_FORM_MISMATCH'
    late = EVAL + timedelta(days=1)
    assert build(filing_body=body(retrieved_at=late)
                 ).filing_body.status == 'BODY_RETRIEVED_AFTER_EVALUATION'
    assert build(filing_body=body(filed_at=late)
                 ).filing_body.status == 'BODY_FILED_AFTER_EVALUATION'
    assert build(filing_body=body(retrieved_at='2026-09-01T00:00:00Z')
                 ).filing_body.status == 'BODY_RETRIEVED_BEFORE_FILING'
    truncated = body(chunks=load('sec_body_8k_5_02.json')['chunks'][:2])
    assert build(filing_body=truncated).filing_body.status == 'BODY_ITEM_MISSING'


def test_unrelated_event_keeps_body_not_applicable():
    result = build(event=event(form='10-Q'))
    assert result.filing_body.status == 'BODY_EVENT_NOT_ELIGIBLE'


def test_tampered_body_text_is_refused_at_validation():
    data = load('sec_body_8k_5_02.json')
    data['chunks'][0]['text'] = data['chunks'][0]['text'] + ' inserted claim'
    with pytest.raises(ValidationError, match='content_hash mismatch'):
        SecFilingBody.model_validate(data)
    data = load('sec_body_8k_5_02.json')
    data['chunks'] = data['chunks'][:1] + data['chunks'][2:]
    with pytest.raises(ValidationError, match='contiguous'):
        SecFilingBody.model_validate(data)
    data = load('sec_body_8k_5_02.json')
    data['rights'] = 'PROPRIETARY'
    with pytest.raises(ValidationError):
        SecFilingBody.model_validate(data)


def test_body_anchor_hit_is_related_not_directional():
    repurchase_body = rewrite_body({
        2: 'The Company announced that its board of directors authorized a share '
           'repurchase program covering up to $200 million of Class A Common Stock. '
           'The program does not change the Company\u2019s tokenized share design.'})
    result = build(filing_body=repurchase_body)
    assert result.thesis_check.relevance == 'RELATED_TO_THESIS'
    assert 'SHARE_REPURCHASE' in result.thesis_check.anchor_hits_in_body
    assert result.thesis_check.next_check_status == 'REVIEW_BODY_ANCHOR'
    assert result.thesis_check.matched_items
    # The check asks for verification; it never asserts direction or magnitude.
    assert 'do not infer direction' in result.thesis_check.next_check
    assert result.valuation.status == 'UNKNOWN'


def test_anchor_outside_the_event_section_is_not_relevance():
    cover = load('sec_body_8k_5_02.json')['chunks'][0]['text']
    result = build(filing_body=rewrite_body({
        0: cover + ' The Company also authorized a share repurchase program.'}))
    assert result.thesis_check.relevance == 'NOT_ADDRESSED'
    assert result.thesis_check.anchor_hits_in_body == ()
    assert result.thesis_check.next_check_status == 'NO_THESIS_ANCHOR_IN_BODY'


def test_unparsed_item_body_is_explicit():
    neutral = rewrite_body({
        2: 'The Company reported no change to its principal accounting officer.',
        3: 'No further disclosure was required for this item.'})
    result = build(filing_body=neutral)
    assert result.filing_body.status == 'BODY_VERIFIED'
    assert result.officer_change.status == 'UNPARSED'
    assert result.thesis_check.next_check_status == 'NO_THESIS_ANCHOR_IN_BODY'
    assert 'the Item 5.02 section' in result.thesis_check.next_check


def test_duplicate_identity_is_refused():
    data = content_from_fixture()
    data['accounts'][1]['account_id'] = 'a0'
    with pytest.raises(ValidationError, match='duplicate account ID'):
        PortfolioSnapshot(**data, snapshot_id='fixture-duplicate-accounts',
                          content_hash='sha256:' + '0' * 64)
    data = content_from_fixture()
    data['positions'].append({**data['positions'][0], 'quantity': '1'})
    with pytest.raises(ValidationError, match='duplicate account/symbol position'):
        PortfolioContent.model_validate(data)


def test_ui_view_is_typed_and_redacts_private_fields():
    shareable = to_ui_view(build(redaction='SHAREABLE_REDACTED'))
    assert shareable.holding.quantity is None
    assert shareable.holding.avg_cost is None
    assert shareable.holding.account_ref == 'REDACTED'
    assert shareable.whatif.cash_before is None
    assert shareable.whatif.cash_after is None
    assert shareable.whatif.gross_value is None
    assert shareable.valuation.value_text is None
    assert shareable.event.accession == '0001859392-26-000105'
    assert shareable.event.body_status == 'BODY_VERIFIED'
    assert 'Dritan Muneka' in shareable.event.officer_summary
    local = to_ui_view(build(redaction='LOCAL_FULL'))
    assert local.holding.quantity == '7'
    assert local.holding.avg_cost == '11.11'
    assert local.whatif.cash_after == '875.00'


def test_ui_view_exposes_the_documented_gap_not_a_recommendation():
    peers = (mark('AAPL', '200'), mark('KLAC', '170'))
    view = to_ui_view(build(price=fresh_price(), other_prices=peers,
                            whatif=None))
    assert view.valuation.status == 'VALUED'
    assert view.valuation.value_text == '175.70'
    assert view.headline.status == 'GAP_DOCUMENTED'
    assert view.thesis.next_check_status == 'NO_THESIS_ANCHOR_IN_BODY'
    codes = {badge.code for badge in view.badges}
    assert {'PRICE_QUALIFIED', 'BODY_VERIFIED', 'INSTRUMENT_VERIFIED',
            'AI_DRAFT_THESIS'} <= codes
    assert 'recommendation' in view.limitations[1]


def test_stale_holding_snapshot_is_disclosed():
    stale_snapshot = rebuild(as_of='2026-10-01T00:00:00Z')
    result = build(snapshot=stale_snapshot, price=fresh_price())
    assert result.holding_scope.holding_freshness == 'STALE'
    assert result.valuation.status == 'VALUED'
    assert result.valuation.current is False
    assert 'HOLDING_SNAPSHOT_IS_STALE' in result.valuation.reasons


def test_shareable_redaction_hides_other_position_symbols():
    shareable = build(redaction='SHAREABLE_REDACTED')
    local = build(redaction='LOCAL_FULL')
    assert shareable.whatif.unpriced_symbols == ()
    assert local.whatif.unpriced_symbols == ('AAPL', 'GLXY', 'KLAC')
    assert shareable.holding.symbol == 'GLXY'


def test_source_receipts_bind_each_input():
    result = build()
    names = {item.name for item in result.provenance}
    assert names == {'owner_holdings', 'sec_event', 'sec_filing_body',
                     'market_price', 'prior_thesis'}
    holdings = next(item for item in result.provenance if item.name == 'owner_holdings')
    assert holdings.content_hash == snapshot().content_hash
    sec = next(item for item in result.provenance if item.name == 'sec_event')
    assert sec.content_hash == event().source_hash
    assert sec.rights == 'PUBLIC_SEC_FILING_METADATA'
    body_receipt = next(item for item in result.provenance
                        if item.name == 'sec_filing_body')
    assert body_receipt.content_hash == body().content_hash
    assert body_receipt.rights == 'PUBLIC_SEC_FILING_BODY'
    assert body_receipt.source_ref == DOC_ID


def test_no_network_is_used(monkeypatch):
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError('network access attempted')

    monkeypatch.setattr(socket, 'socket', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    result = build()
    assert result.whatif.external_send is False


def test_extractor_refuses_live_database_path(tmp_path):
    output = tmp_path / 'slice.json'
    command = [sys.executable, str(ROOT / 'scripts' / 'extract_owner_value_003.py'),
               '--database', str(ROOT / 'data' / 'market_agent.db'),
               '--account-id', 'a0', '--symbol', 'GLXY',
               '--sec-capture', str(FIXTURES / 'sec' / 'GLXY_capture.json'),
               '--output', str(output)]
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
    env.pop('MIA_DB_PATH', None)
    done = subprocess.run(command, capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 2
    assert 'LIVE_DATABASE_REFUSED_COPY_REQUIRED' in done.stderr
    assert not output.exists()


def sandbox_copy(path, *, with_body=False, theses=None):
    """Shape a synthetic SQLite copy like the Owner DB; never the live DB."""
    import sqlite3
    connection = sqlite3.connect(path)
    connection.executescript(
        'create table portfolio_snapshots (snapshot_id text, as_of text, '
        'content_hash text, payload text);'
        'create table thesis_versions (thesis_version_id text, symbol text, as_of text, '
        'status text, evidence_id text, payload text, evidence_payload text);'
        'create table provider_cache (provider text, symbol text, requested_as_of text, '
        'observation_as_of text, retrieved_at text, payload text);')
    snapshot_payload = load('snapshot_primary.json')
    connection.execute('insert into portfolio_snapshots values (?,?,?,?)',
                       (snapshot_payload['snapshot_id'], snapshot_payload['as_of'],
                        snapshot_payload['content_hash'], json.dumps(snapshot_payload)))
    versions = theses or ({'payload': load('thesis_ai_draft.json'),
                           'retrieved_at': '2026-08-12T15:00:00Z'},)
    for version in versions:
        thesis_payload = version['payload']
        connection.execute(
            'insert into thesis_versions values (?,?,?,?,?,?,?)',
            (thesis_payload['thesis_version_id'], thesis_payload['symbol'],
             thesis_payload['as_of'], thesis_payload['status'],
             thesis_payload['evidence_id'], json.dumps(thesis_payload),
             json.dumps({'evidence_id': thesis_payload['evidence_id'],
                         'retrieved_at': version['retrieved_at']})))
    quote = load('price_stale_massive.json')
    cache_payload = {
        'observations': [{'symbol': quote['symbol'], 'price': quote['price'],
                          'as_of': quote['observed_at'], 'currency': quote['currency'],
                          'provider': quote['provider'], 'evidence_id': quote['evidence_id']}],
        'evidence': [{'evidence_id': quote['evidence_id'], 'provider': quote['provider'],
                      'source_uri': quote['source_uri'],
                      'retrieved_at': quote['retrieved_at'],
                      'structured_data': {'price_basis': 'unadjusted'}}],
    }
    connection.execute('insert into provider_cache values (?,?,?,?,?,?)',
                       (quote['provider'], quote['symbol'], quote['observed_at'],
                        quote['observed_at'], quote['retrieved_at'],
                        json.dumps(cache_payload)))
    if with_body:
        connection.executescript(
            'create table sec_documents (document_id text, accession_number text, '
            'cik text, form text, filed_at text, retrieved_at text, '
            'source_uri text, content_hash text);'
            'create table sec_chunks (document_id text, sequence integer, section text, '
            'text text, content_hash text);')
        filing = load('sec_body_8k_5_02.json')
        connection.execute('insert into sec_documents values (?,?,?,?,?,?,?,?)',
                           (filing['document_id'], filing['accession'], filing['cik'],
                            filing['form'], filing['filed_at'], filing['retrieved_at'],
                            filing['source_uri'], filing['content_hash']))
        connection.executemany(
            'insert into sec_chunks values (?,?,?,?,?)',
            [(filing['document_id'], chunk['sequence'], chunk['section'],
              chunk['text'], chunk['content_hash']) for chunk in filing['chunks']])
    connection.commit()
    connection.close()


def extract(tmp_path, database):
    output = tmp_path / 'slice.json'
    command = [sys.executable, str(ROOT / 'scripts' / 'extract_owner_value_003.py'),
               '--database', str(database), '--account-id', 'a0', '--symbol', 'GLXY',
               '--sec-capture', str(FIXTURES / 'sec' / 'GLXY_capture.json'),
               '--evaluation-time', EVAL.isoformat(),
               '--whatif-side', 'BUY', '--whatif-shares', '5',
               '--whatif-price', '25.00',
               '--whatif-rationale', 'Illustrative sizing only; not an order.',
               '--output', str(output)]
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
    env.pop('MIA_DB_PATH', None)
    done = subprocess.run(command, capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr
    return output, json.loads(output.read_text(encoding='utf-8'))


def test_extractor_reads_sandbox_copy_without_writing(tmp_path):
    database = tmp_path / 'sandbox-copy.db'
    sandbox_copy(database)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    _, result = extract(tmp_path, database)
    assert before == hashlib.sha256(database.read_bytes()).hexdigest()
    assert result['extraction']['database_writes'] == 0
    assert result['extraction']['network_calls'] == 0
    assert result['extraction']['sec_body_status'] == 'BODY_NOT_PROVIDED'
    assert result['extraction']['sec_body_document_id'] is None
    assert result['extraction']['sec_body_chunks'] == 0
    assert result['slice']['holding']['quantity'] == '7'
    assert result['slice']['event']['status'] == 'ELIGIBLE'
    assert result['slice']['price']['status'] == 'STALE'
    assert result['slice']['valuation']['status'] == 'UNKNOWN'
    assert result['slice']['thesis_check']['status'] == 'THESIS_AVAILABLE'
    assert result['slice']['thesis_check']['next_check_status'] == 'REQUIRES_DOCUMENT_BODY'
    assert result['slice']['filing_body']['status'] == 'BODY_NOT_PROVIDED'
    assert result['slice']['whatif']['cash_after'] == '875.00'


def test_extractor_uses_captured_filing_body_from_copy(tmp_path):
    database = tmp_path / 'sandbox-copy.db'
    sandbox_copy(database, with_body=True)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    _, result = extract(tmp_path, database)
    assert before == hashlib.sha256(database.read_bytes()).hexdigest()
    assert result['extraction']['sec_body_status'] == 'BODY_VERIFIED'
    assert result['extraction']['sec_body_document_id'] == 'SDa7082bd82c9b0c78'
    assert result['extraction']['sec_body_chunks'] == 4
    assert result['slice']['filing_body']['status'] == 'BODY_VERIFIED'
    assert result['slice']['instrument']['share_class'] == 'CLASS_A'
    assert result['slice']['instrument']['symbol'] == 'GLXY'
    assert result['slice']['officer_change']['status'] == 'PARSED'
    assert result['slice']['thesis_check']['relevance'] == 'NOT_ADDRESSED'
    assert result['slice']['thesis_check']['next_check_status'] == 'NO_THESIS_ANCHOR_IN_BODY'
    assert result['slice']['valuation']['status'] == 'UNKNOWN'
    assert result['slice']['whatif']['cash_after'] == '875.00'


# --- P1 A regressions: the thesis must be knowledge the Owner actually had ----


def test_thesis_at_the_acceptance_instant_is_not_prior_knowledge():
    accepted = event().accepted_at
    result = build(thesis=thesis(as_of=accepted))
    assert result.thesis_check.status == 'THESIS_AFTER_EVENT'
    assert result.thesis_check.thesis_boundary == accepted
    assert result.thesis_check.thesis_as_of == accepted


def test_future_thesis_is_refused_even_without_an_event():
    result = build(event=None, thesis=thesis(as_of=EVAL + timedelta(days=30)))
    assert result.thesis_check.status != 'THESIS_AVAILABLE'
    assert result.thesis_check.thesis_boundary == EVAL
    assert 'THESIS_MUST_STRICTLY_PREDATE_EVENT_AND_EVALUATION' in result.thesis_check.reasons


def test_ineligible_event_still_bounds_the_thesis_by_evaluation_time():
    late = event(accepted_at=EVAL + timedelta(hours=1))
    result = build(event=late, thesis=thesis(as_of=EVAL + timedelta(minutes=1)))
    assert result.event.status == 'PIT_INELIGIBLE_NOT_ACCEPTED_BY_EVALUATION'
    assert result.thesis_check.status == 'THESIS_AFTER_EVENT'
    assert result.thesis_check.thesis_boundary == EVAL


def test_backdated_thesis_recorded_after_the_event_is_refused():
    result = build(thesis=thesis(
        as_of=datetime(2026, 9, 1, tzinfo=timezone.utc),
        stored_available_at=datetime(2026, 10, 26, tzinfo=timezone.utc)))
    assert result.thesis_check.status == 'THESIS_NOT_AVAILABLE'
    assert 'THESIS_RECORDED_AFTER_BOUNDARY' in result.thesis_check.reasons
    assert result.thesis_check.thesis_stored_available_at == \
        datetime(2026, 10, 26, tzinfo=timezone.utc)


def test_thesis_without_stored_availability_is_not_prior_knowledge():
    result = build(thesis=thesis(stored_available_at=None))
    assert result.thesis_check.status == 'THESIS_NOT_AVAILABLE'
    assert 'THESIS_STORED_AVAILABILITY_UNKNOWN' in result.thesis_check.reasons


def test_extractor_selects_the_latest_genuinely_eligible_thesis(tmp_path):
    database = tmp_path / 'sandbox-copy.db'
    older = load('thesis_ai_draft.json')
    backfilled = {**older, 'thesis_version_id': 'TVFIXTURE0000000002',
                  'as_of': '2026-09-20T00:00:00Z'}
    sandbox_copy(database, theses=(
        {'payload': backfilled, 'retrieved_at': '2026-10-26T00:00:00Z'},
        {'payload': older, 'retrieved_at': '2026-08-12T15:00:00Z'}))
    _, result = extract(tmp_path, database)
    selection = result['extraction']['thesis_selection']
    assert selection['thesis_version_id'] == 'TVFIXTURE0000000001'
    assert selection['rejected_after_boundary'] == 1
    assert result['slice']['thesis_check']['status'] == 'THESIS_AVAILABLE'
    assert result['slice']['thesis_check']['thesis_version_id'] == 'TVFIXTURE0000000001'


def test_extractor_reports_no_thesis_when_every_version_was_recorded_late(tmp_path):
    database = tmp_path / 'sandbox-copy.db'
    backfilled = {**load('thesis_ai_draft.json'), 'as_of': '2026-09-20T00:00:00Z'}
    sandbox_copy(database, theses=(
        {'payload': backfilled, 'retrieved_at': '2026-10-26T00:00:00Z'},))
    _, result = extract(tmp_path, database)
    assert result['extraction']['thesis_selection']['reason'] == 'RECORDED_AFTER_BOUNDARY'
    assert result['slice']['thesis_check']['status'] == 'THESIS_NOT_AVAILABLE'


# --- P1 B regressions: only fully qualified marks may reach NAV or a weight ----


def test_unqualified_peer_mark_never_raises_priced_count_or_nav():
    stale_mark = mark('AAPL', '200', observed_at='2026-09-01T00:00:00Z')
    result = build(price=fresh_price(),
                   other_prices=(stale_mark, mark('KLAC', '170')))
    assert result.account_scope.priced_position_count == 2
    receipt = result.whatif
    assert receipt.nav_basis == 'UNKNOWN'
    assert receipt.nav_before is None
    assert receipt.focus_weight_before is None
    assert receipt.qualified_mark_count == 2
    assert receipt.unqualified_mark_count == 1
    assert receipt.unpriced_position_count == 1


def test_hypothetical_focus_price_never_becomes_a_nav_mark():
    result = build(price=stale_price(),
                   other_prices=(mark('AAPL', '200'), mark('KLAC', '170')))
    receipt = result.whatif
    assert receipt.mode == 'HYPOTHETICAL_SCENARIO'
    assert receipt.scenario_price == Decimal('25.00')
    assert receipt.nav_basis == 'UNKNOWN'
    assert receipt.nav_after is None
    assert result.account_scope.priced_position_count == 2
    assert result.valuation.status == 'UNKNOWN'
    assert result.valuation.market_value is None


def test_currency_and_rights_blocked_peer_marks_are_not_converted():
    result = build(price=fresh_price(),
                   other_prices=(mark('AAPL', '200', currency='EUR'),
                                 mark('KLAC', '170', usage_rights='DISPLAY_ONLY')))
    assert result.whatif.nav_basis == 'UNKNOWN'
    assert result.whatif.unqualified_mark_count == 2
    assert result.account_scope.priced_position_count == 1


def test_peer_mark_contract_refuses_ambiguous_inputs():
    with pytest.raises(SliceRefused, match='MARK_SYMBOL_NOT_HELD_IN_ACCOUNT'):
        build(other_prices=(mark('MSFT', '100'),))
    with pytest.raises(SliceRefused, match='OTHER_MARKS_MUST_EXCLUDE_FOCUS_SYMBOL'):
        build(other_prices=(mark('GLXY', '25.10'),))
    with pytest.raises(SliceRefused, match='DUPLICATE_MARK_OBSERVATION'):
        build(other_prices=(mark('AAPL', '200'), mark('AAPL', '201')))


# --- P1 C regressions: the live Owner DB is refused from any checkout ---------


def load_extractor_module():
    import importlib.util
    path = ROOT / 'scripts' / 'extract_owner_value_003.py'
    spec = importlib.util.spec_from_file_location('extract_owner_value_003_under_test',
                                                  path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_live_database_guard_follows_the_repository_not_the_script_location(tmp_path,
                                                                           monkeypatch):
    if shutil.which('git') is None:
        pytest.skip('git is required to resolve the shared repository root')
    extractor = load_extractor_module()
    repository = tmp_path / 'canonical'
    (repository / 'tools' / 'data').mkdir(parents=True)
    (repository / 'data').mkdir()
    live = repository / 'data' / 'market_agent.db'
    live.write_bytes(b'synthetic placeholder; never the Owner DB')
    subprocess.run(['git', 'init', '--quiet', str(repository)], check=True, timeout=60)
    monkeypatch.setattr(extractor, 'ROOT', repository / 'tools')
    monkeypatch.setattr(extractor, 'DEFAULT_DATABASE',
                        repository / 'tools' / 'data' / 'market_agent.db')
    monkeypatch.delenv('MIA_DB_PATH', raising=False)
    assert extractor.is_live_database(live) is True
    sandbox = tmp_path / 'sandbox-copy.db'
    sandbox.write_bytes(b'synthetic copy')
    assert extractor.is_live_database(sandbox) is False


def test_live_database_guard_always_covers_the_default_database():
    extractor = load_extractor_module()
    assert extractor.DEFAULT_DATABASE == ROOT / 'data' / 'market_agent.db'
    assert extractor.is_live_database(extractor.DEFAULT_DATABASE) is True


def test_live_database_guard_detects_hardlink_and_configured_path(tmp_path, monkeypatch):
    extractor = load_extractor_module()
    monkeypatch.setattr(extractor, 'ROOT', tmp_path / 'script-checkout')
    monkeypatch.setattr(extractor, 'DEFAULT_DATABASE',
                        tmp_path / 'script-checkout' / 'data' / 'market_agent.db')
    live = tmp_path / 'owner-live.db'
    live.write_bytes(b'synthetic placeholder')
    monkeypatch.setenv('MIA_DB_PATH', str(live))
    alias = tmp_path / 'alias.db'
    try:
        os.link(live, alias)
    except OSError:
        pytest.skip('hardlinks are unavailable on this filesystem')
    assert extractor.is_live_database(alias) is True
    copy = tmp_path / 'sandbox-copy.db'
    copy.write_bytes(b'synthetic copy')
    assert extractor.is_live_database(copy) is False


def test_extractor_refuses_a_database_named_by_mia_db_path(tmp_path):
    database = tmp_path / 'owner-live.db'
    sandbox_copy(database)
    output = tmp_path / 'slice.json'
    command = [sys.executable, str(ROOT / 'scripts' / 'extract_owner_value_003.py'),
               '--database', str(database), '--account-id', 'a0', '--symbol', 'GLXY',
               '--sec-capture', str(FIXTURES / 'sec' / 'GLXY_capture.json'),
               '--output', str(output)]
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'),
           'MIA_DB_PATH': str(database)}
    done = subprocess.run(command, capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 2
    assert 'LIVE_DATABASE_REFUSED_COPY_REQUIRED' in done.stderr
    assert not output.exists()


def test_extractor_refuses_the_canonical_database_seen_from_another_checkout(tmp_path):
    if shutil.which('git') is None:
        pytest.skip('git is required to resolve the shared repository root')
    repository = tmp_path / 'canonical'
    (repository / 'tools' / 'scripts').mkdir(parents=True)
    (repository / 'data').mkdir()
    live = repository / 'data' / 'market_agent.db'
    live.write_bytes(b'synthetic placeholder; never the Owner DB')
    shutil.copy2(ROOT / 'scripts' / 'extract_owner_value_003.py',
                 repository / 'tools' / 'scripts' / 'extract_owner_value_003.py')
    subprocess.run(['git', 'init', '--quiet', str(repository)], check=True, timeout=60)
    output = tmp_path / 'slice.json'
    command = [sys.executable,
               str(repository / 'tools' / 'scripts' / 'extract_owner_value_003.py'),
               '--database', str(live), '--account-id', 'a0', '--symbol', 'GLXY',
               '--sec-capture', str(FIXTURES / 'sec' / 'GLXY_capture.json'),
               '--output', str(output)]
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
    env.pop('MIA_DB_PATH', None)
    done = subprocess.run(command, capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 2
    assert 'LIVE_DATABASE_REFUSED_COPY_REQUIRED' in done.stderr
    assert not output.exists()
