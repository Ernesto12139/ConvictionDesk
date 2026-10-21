"""Bounded saved-current screening and observable aging; scripted public oracles."""
import asyncio
import time
from datetime import timedelta

from fastapi.testclient import TestClient
import pytest

from test_convictiondesk_attention import NOW, service
from market_agent.domain import EvidenceItem
from market_agent.research_universe import UniverseChange
from market_agent.tdb import content_hash


def receipt(symbol, title, published, retrieved, record):
    data={'title':title,'text':'Synthetic operating disclosure '+str(record)}
    digest=content_hash(data)
    return EvidenceItem(evidence_id='E'+digest[7:23],evidence_type='news',provider='synthetic-public',
        symbol=symbol,title=title,excerpt=data['text'],published_at=published,retrieved_at=retrieved,
        source_record_id=str(record),source_uri=f'https://example.com/{symbol}/{record}',
        content_hash=digest)


def test_older_current_symbol_is_not_starved_by_daily_new_symbols(tmp_path):
    attention,news,model=service(tmp_path)
    for symbol in ['SMR','AAPL','MSFT','NVDA']:
        attention.research.change(UniverseChange(symbol=symbol,state='WATCHING'),NOW)
    critical=receipt('SMR','Synthetic regulator suspends review; guidance withdrawn',
        NOW-timedelta(hours=20),NOW,'critical')
    for day in range(9):
        now=NOW+timedelta(days=day);attention.clock=lambda:now
        news.rows=([critical] if day==0 else [])+[
            receipt(symbol,'Synthetic routine note',now-timedelta(minutes=k+1),now,f'{day}-{symbol}-{k}')
            for symbol in ['APP','AAPL','MSFT','NVDA'] for k in range(2)]
        asyncio.run(attention.scan(*attention.begin()))
    screened=[event['evidence'][0]['evidence_id'] for request in model.requests for event in request.facts['events']]
    assert critical.evidence_id in screened,'Recent evidence expired unscreened behind repetitive new-day arrivals'
    assert all(len(r.facts['events'])<=4 for r in model.requests)


def test_current_holding_receipt_leads_watch_receipts_without_filling_old_slots(tmp_path):
    from market_agent.attention import EventPacket
    from market_agent.attention_selection import select_candidates
    attention,news,_=service(tmp_path)
    entry=attention.research.entries(NOW)[0]
    attention.ingest('synthetic',entry,news.rows,NOW,NOW,complete=False)
    with attention.store._connect() as db:
        packets=[EventPacket.model_validate_json(row[0]) for row in db.execute('SELECT payload FROM attention_events')]
    holding=packets[0].model_copy(update={'universe':entry.model_copy(update={'state':'HOLDING'})})
    selected=select_candidates([packets[-1],holding],NOW,1)
    assert selected[0].universe.state=='HOLDING'


def test_queue_live_lifecycle_distinguishes_expired_from_already_historical(tmp_path):
    attention,news,_=service(tmp_path)
    entry=attention.research.entries(NOW)[0]
    attention.ingest('current',entry,news.rows[:2],NOW,NOW,complete=False)
    old=receipt('APP','Synthetic already historical',NOW-timedelta(days=20),NOW,'old')
    attention.ingest('older',entry,[old],NOW-timedelta(days=15),NOW,complete=False)
    with attention.store._connect() as db:
        before=db.execute('SELECT payload,payload_hash,screen FROM attention_events ORDER BY rowid').fetchall()
    attention.clock=lambda:NOW+timedelta(days=8)
    from market_agent.main import create_app
    # The registered queue reads the same service; no scan/LLM/provider on GET.
    app=create_app(db_path=attention.store.path,market_fixture='evals/fixtures/market_eod.json',environment={})
    app.state.attention_service.clock=attention.clock
    with TestClient(app) as client:
        data=client.get('/api/attention/queue').json()['pending_lifecycle']
    assert data['expired_unscreened']==2
    assert data['historical_other']==1 and data['current']==0
    assert data['by_symbol']['APP']['expired_unscreened']==2
    with attention.store._connect() as db:
        assert before==db.execute('SELECT payload,payload_hash,screen FROM attention_events ORDER BY rowid').fetchall()


def test_saved_current_continuation_drains_without_fetch_or_cursor_change(tmp_path):
    attention,news,model=service(tmp_path)
    first=asyncio.run(attention.scan(*attention.begin()))
    assert first['screened']==4
    calls=[]
    def no_fetch(*args):calls.append(args);raise AssertionError('Continuation fetched a provider')
    news.search=no_fetch
    with attention.store._connect() as db:
        cursor=db.execute("SELECT value FROM research_meta WHERE name='attention_batch_cursor_v1'").fetchone()
        frozen=db.execute('SELECT payload,payload_hash FROM attention_events ORDER BY rowid').fetchall()
    second=asyncio.run(attention.scan(*attention.begin(),fetch_sources=False))
    third=asyncio.run(attention.scan(*attention.begin(),fetch_sources=False))
    empty=asyncio.run(attention.scan(*attention.begin(),fetch_sources=False))
    assert [second['screened'],third['screened'],empty['screened']]==[4,1,0]
    assert not calls and len(model.requests)==3
    assert second['scan_mode']=='SAVED_CURRENT_ONLY' and second['source_fetch_performed'] is False
    assert second['symbols']==0 and second['result_status']=='PARTIAL'
    assert all(row['status']=='NOT_FETCHED_IN_THIS_SCAN' for row in second['symbol_coverage'].values())
    assert third['pending_current_events']==0
    with attention.store._connect() as db:
        assert cursor==db.execute("SELECT value FROM research_meta WHERE name='attention_batch_cursor_v1'").fetchone()
        assert frozen==db.execute('SELECT payload,payload_hash FROM attention_events ORDER BY rowid').fetchall()


def test_continuation_failure_keeps_pending_without_retry_or_provider_fetch(tmp_path):
    attention,news,model=service(tmp_path)
    asyncio.run(attention.scan(*attention.begin()))
    model.fail=True
    news.search=lambda *args: (_ for _ in ()).throw(AssertionError('Provider fetch forbidden'))
    report=asyncio.run(attention.scan(*attention.begin(),fetch_sources=False))
    assert report['pending_current_events']==5 and report['screened']==0
    assert len(model.requests)==2 and report['result_status']=='PARTIAL'
    assert any(e.get('stage')=='screen' for e in report['errors'])


def test_empty_continuation_cannot_claim_a_complete_source_scan(tmp_path):
    attention,news,model=service(tmp_path)
    attention.research.change(UniverseChange(symbol='APP',state='ARCHIVED'),NOW)
    report=asyncio.run(attention.scan(*attention.begin(),fetch_sources=False))
    assert report['result_status']=='PARTIAL'
    assert report['coverage_incomplete'] and report['status']=='COMPLETED_WITH_LIMITATIONS'
    assert not model.requests


def test_real_api_get_never_starts_continue_and_post_is_one_bounded_batch(tmp_path):
    from market_agent.main import create_app
    attention,news,model=service(tmp_path)
    asyncio.run(attention.scan(*attention.begin()))
    news.search=lambda *args: (_ for _ in ()).throw(AssertionError('Provider fetch forbidden'))
    app=create_app(db_path=attention.store.path,market_fixture='evals/fixtures/market_eod.json',
        news_provider=news,llm_client=model,clock=lambda:NOW,environment={})
    with TestClient(app) as client:
        for _ in range(3):
            assert client.get('/api/attention/queue').json()['pending_lifecycle']['current']==5
        assert len(model.requests)==1
        response=client.post('/api/attention/continue')
        assert response.status_code==202,response.text
        scan_id=response.json()['scan_id']
        for _ in range(150):
            result=client.get('/api/attention/scans/'+scan_id).json()
            if result['status']!='RUNNING':break
            time.sleep(.01)
        assert result['report']['screened']==4
        assert result['report']['source_fetch_performed'] is False
        assert client.get('/api/attention/queue').json()['pending_lifecycle']['current']==1
        assert len(model.requests)==2


def test_cancelled_continuation_retains_mode_pending_and_only_explicit_retry(tmp_path):
    attention,news,model=service(tmp_path)
    asyncio.run(attention.scan(*attention.begin()))
    original=model.complete_structured
    news.search=lambda *args: (_ for _ in ()).throw(AssertionError('Provider fetch forbidden'))
    async def interrupt():
        entered=asyncio.Event()
        async def waiting(request):
            model.requests.append(request);entered.set()
            await asyncio.Event().wait()
        model.complete_structured=waiting
        scan_id,cutoff,entries=attention.begin()
        job=asyncio.create_task(attention.scan(scan_id,cutoff,entries,fetch_sources=False))
        await asyncio.wait_for(entered.wait(),2)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):await job
        result=attention.research.report(scan_id)
        assert result['status']=='INTERRUPTED'
        assert result['report']['scan_mode']=='SAVED_CURRENT_ONLY'
        assert result['report']['pending_events']==5
    asyncio.run(interrupt())
    assert len(model.requests)==2,'Cancellation must not retry the model automatically'
    model.complete_structured=original
    report=asyncio.run(attention.scan(*attention.begin(),fetch_sources=False))
    assert report['screened']==4 and report['pending_current_events']==1
    assert len(model.requests)==3


def test_current_universe_overrides_frozen_old_holding_state(tmp_path):
    from market_agent.attention import EventPacket
    from market_agent.attention_selection import select_candidates
    attention,news,_=service(tmp_path)
    entry=attention.research.entries(NOW)[0]
    attention.ingest('synthetic',entry,news.rows,NOW,NOW,complete=False)
    with attention.store._connect() as db:
        packet=EventPacket.model_validate_json(db.execute('SELECT payload FROM attention_events LIMIT 1').fetchone()[0])
    previously_held=packet.model_copy(update={'universe':entry.model_copy(update={'state':'HOLDING'})})
    now_held=packet.model_copy(update={'universe':entry.model_copy(update={'symbol':'SMR','state':'WATCHING'}),
        'evidence':[packet.evidence[0].model_copy(update={'symbol':'SMR'})]})
    selected=select_candidates([previously_held,now_held],NOW,1,holding_symbols={'SMR'})
    assert selected[0].universe.symbol=='SMR'
