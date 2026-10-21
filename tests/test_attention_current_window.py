"""Manual current intelligence must not spend its screen budget on old backlog."""
import asyncio
from datetime import timedelta

from test_convictiondesk_attention import NOW, service


def seeded_backlog(tmp_path):
    attention, news, model = service(tmp_path)
    entry = attention.research.entries(NOW)[0]
    attention.ingest('prior-window', entry, news.rows, NOW, NOW, complete=False)
    current = NOW + timedelta(days=8)
    attention.clock = lambda: current
    news.rows = []
    return attention, news, model, current


def test_historical_only_pending_is_retained_without_model_screening(tmp_path):
    attention, _, model, _ = seeded_backlog(tmp_path)
    report = asyncio.run(attention.scan(*attention.begin()))
    assert model.requests == [], 'Historical backlog consumed the manual current-news model budget'
    assert report['pending_events'] == 9
    assert report['pending_current_events'] == 0
    assert report['pending_historical_events'] == 9
    assert report['result_status'] == 'PARTIAL'
    assert report['current_batch_result'] == 'COMPLETED_NO_MATERIAL_EVENTS'


def test_one_new_receipt_never_fills_screen_packet_with_old_backlog(tmp_path):
    attention, news, model, current = seeded_backlog(tmp_path)
    from market_agent.domain import EvidenceItem
    from market_agent.tdb import content_hash
    payload = {'title': 'APP new operating disclosure', 'text': 'New source, synthetic only.'}
    digest = content_hash(payload)
    news.rows = [EvidenceItem(evidence_id='E'+digest[7:23], evidence_type='news',
        provider='synthetic', symbol='APP', title=payload['title'], excerpt=payload['text'],
        source_uri='https://example.com/new-window', source_record_id='new-window',
        published_at=current-timedelta(minutes=1), retrieved_at=current, content_hash=digest)]
    report = asyncio.run(attention.scan(*attention.begin()))
    events = model.requests[0].facts['events']
    assert len(events) == 1, 'Old evidence filled unused current screening slots'
    assert events[0]['evidence'][0]['evidence_id'] == news.rows[0].evidence_id
    assert report['screened'] == 1 and report['pending_events'] == 9
    assert report['pending_historical_events'] == 9
    assert report['symbol_coverage']['APP']['status'] == 'COMPLETE_WINDOW'
    repeated = asyncio.run(attention.scan(*attention.begin()))
    assert repeated['new_events'] == 0 and repeated['duplicates'] == 1
    assert len(model.requests) == 1, 'Duplicate current receipt triggered historical screening'


def test_source_failure_is_unavailable_even_when_historical_backlog_exists(tmp_path):
    attention, news, model, _ = seeded_backlog(tmp_path)
    news.fail = True
    report = asyncio.run(attention.scan(*attention.begin()))
    assert report['result_status'] == report['current_batch_result'] == 'PROVIDER_UNAVAILABLE'
    assert report['pending_events'] == 9 and not model.requests
    assert report['symbol_coverage']['APP']['status'] == 'PROVIDER_UNAVAILABLE'


def test_current_window_selection_keeps_newest_and_both_symbols_bounded(tmp_path):
    from market_agent.attention_selection import select_candidates, current_candidate
    from market_agent.attention import EventPacket
    from market_agent.research_universe import UniverseChange
    attention, news, _, current = seeded_backlog(tmp_path)
    attention.research.change(UniverseChange(symbol='SMR',state='WATCHING'),current)
    entries={e.symbol:e for e in attention.research.entries(current)}
    with attention.store._connect() as db:
        frozen=[EventPacket.model_validate_json(row[0]) for row in db.execute('SELECT payload FROM attention_events')]
    recent=[]
    for index, symbol in enumerate(['APP','APP','SMR']):
        receipt=frozen[index].evidence[0].model_copy(update={'symbol':symbol,
            'published_at':current-timedelta(minutes=index), 'retrieved_at':current})
        recent.append(frozen[index].model_copy(update={'universe':entries[symbol],'evidence':[receipt]}))
    chosen=select_candidates([p for p in [*frozen,*recent] if current_candidate(p,current)],current,2)
    assert {p.universe.symbol for p in chosen} == {'APP','SMR'}
    assert chosen[0].universe.symbol=='APP', 'Fresh lane must offer the new arrival before older routine volume'
    assert chosen[1].universe.symbol=='SMR', 'Deadline lane must still reserve capacity for the older symbol'
