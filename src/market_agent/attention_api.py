"""One backend capability shared by console and CLI; persisted polling/readback."""
import asyncio
import logging

from fastapi import HTTPException
from pydantic import Field

from .attention import TOP_N
from .attention_groups import project_deep
from .domain import FrozenModel, utc
from .research_universe import UniverseChange
from .tdb import TDBError


class ReviewUnitRequest(FrozenModel):
    member_item_ids: list[str] = Field(min_length=1, max_length=1000)


def register_attention(app,service):
    research=service.research
    research.recover_interrupted()
    tasks=set()
    app.state.attention_service=service

    def launch(coroutine):
        task=asyncio.create_task(coroutine)
        tasks.add(task)
        def done(t):
            tasks.discard(t)
            if not t.cancelled() and t.exception():
                # Exception prose can contain provider/private material; only type.
                logging.getLogger(__name__).error('Attention task failed: %s',type(t.exception()).__name__)
        task.add_done_callback(done)

    def readback(deep, item_id):
        if not deep:
            return None
        # One observed queue projection per read. A later scan can supersede it;
        # subsequent polling/refresh rechecks, with no global linearization claim.
        row = next((r for r in research.queue(utc(service.clock()),None)
                    if item_id in r['research_event']['all_member_item_ids']), None)
        return project_deep(research, deep, row)

    @app.get('/api/research-universe')
    def universe():
        return {'entries':[e.model_dump(mode='json') for e in research.entries(utc(service.clock()))]}

    @app.put('/api/research-universe')
    def change(request:UniverseChange):
        return research.change(request,utc(service.clock()))

    @app.post('/api/attention/scan',status_code=202)
    async def scan():
        scan_id,cutoff,entries=service.begin()
        launch(service.scan(scan_id,cutoff,entries))
        return {'scan_id':scan_id,'status':'RUNNING'}

    @app.post('/api/attention/continue',status_code=202)
    async def continue_current():
        scan_id,cutoff,entries=service.begin()
        launch(service.scan(scan_id,cutoff,entries,fetch_sources=False))
        return {'scan_id':scan_id,'status':'RUNNING','scan_mode':'SAVED_CURRENT_ONLY'}

    @app.get('/api/attention/scans/{scan_id}')
    def scan_status(scan_id:str):
        return research.report(scan_id)

    @app.get('/api/attention/queue')
    def queue():
        from .market_evidence import attention_freshness, NEW_YORK
        now=utc(service.clock())
        units=research.queue(now,None)
        decorated=[{**row, 'freshness': attention_freshness(row, now)} for row in units]
        current=[row for row in decorated if row['freshness']['freshness_status'] in {'TODAY','RECENT'}]
        historical=[row for row in decorated if row['freshness']['freshness_status'] not in {'TODAY','RECENT'}]
        rows=current[:TOP_N]
        entries=[e for e in research.entries(now) if e.state!='ARCHIVED']
        active={e.symbol for e in entries}
        with service.store._connect() as db:
            latest=db.execute('SELECT scan_id FROM attention_scans ORDER BY started_at DESC,rowid DESC LIMIT 1').fetchone()
            successful=db.execute("SELECT scan_id FROM attention_scans WHERE status='COMPLETED' ORDER BY started_at DESC,rowid DESC LIMIT 1").fetchone()
            counts=db.execute('SELECT symbol,COUNT(*) FROM attention_events WHERE screen IS NULL GROUP BY symbol').fetchall()
            screened_counts=db.execute('SELECT symbol,COUNT(*) FROM attention_events WHERE screen IS NOT NULL GROUP BY symbol').fetchall()
            pending_rows=db.execute('SELECT symbol,payload,payload_hash FROM attention_events WHERE screen IS NULL').fetchall()
        pending=sum(count for symbol,count in counts if symbol in active)
        inactive=sum(count for symbol,count in counts if symbol not in active)
        latest_scan = research.report(latest[0]) if latest else None
        latest_successful = research.report(successful[0]) if successful else None
        from .attention_selection import scan_outcome
        from .attention_selection import pending_lifecycle
        from .attention import EventPacket
        lifecycle=pending_lifecycle([EventPacket.model_validate(research.checked(payload,digest))
            for symbol,payload,digest in pending_rows if symbol in active],now)
        coverage = {'ranking_set': 'SCREENED_UNRESOLVED_RESEARCH_EVENTS',
            'screened_records': sum(count for symbol,count in screened_counts if symbol in active),
            'unresolved_events': len(units), 'shown_events': len(rows),
            'unresolved_records': sum(len(r['research_event']['member_item_ids']) for r in units),
            'pending_records': pending, 'full_universe_ranking': False}
        return {'items':rows,'historical_items':historical[:TOP_N],
                'freshness':{'as_of':now.isoformat(),'market_timezone':'America/New_York',
                    'publication_date':now.astimezone(NEW_YORK).date().isoformat(),
                    'current_events':len(current),'historical_events':len(historical),
                    'meaning':'Publication date, not verified event occurrence or trading day'},
                'latest_scan':latest_scan,'latest_successful_scan':latest_successful,
                'scan_result_status':scan_outcome(latest_scan.get('report') if latest_scan else None),
                'pending_events':pending,'coverage':coverage,
                'pending_lifecycle':lifecycle,
                'inactive_pending_events':inactive,
                'scan_scope':service.scan_scope(entries),
                'screen_batch_limit':service.screen_batch_size,
                'jev_mode':'DEFERRED','ranking_scope':'Research attention only; cost exposure is not current market weight'}

    @app.get('/api/attention/items/{item_id}')
    def item(item_id:str):
        return {'item':research.item(item_id),'deep':readback(research.latest_deep(item_id),item_id)}

    @app.post('/api/attention/items/{item_id}/reviewed')
    def reviewed(item_id:str, request:ReviewUnitRequest | None = None):
        research.item(item_id)
        ids = request.member_item_ids if request else [item_id]
        if len(ids) != len(set(ids)) or item_id not in ids:
            raise TDBError('ATTENTION_REVIEW_MEMBERSHIP_INVALID',409)
        if request:
            row = next((r for r in research.queue(utc(service.clock()),None)
                        if item_id in r['research_event']['all_member_item_ids']),None)
            if not row or set(ids)-set(row['research_event']['all_member_item_ids']):
                raise TDBError('ATTENTION_REVIEW_MEMBERSHIP_INVALID',409)
        with service.store._connect() as db:
            db.executemany('INSERT OR IGNORE INTO attention_reviewed VALUES (?,?)',
                           [(key,utc(service.clock()).isoformat()) for key in ids])
        return {'item_id':item_id,'status':'REVIEWED'}

    @app.post('/api/attention/items/{item_id}/deep',status_code=202)
    async def deep(item_id:str):
        prior=research.latest_deep(item_id)
        if prior and prior['status'] in {'RUNNING','COMPLETED'}:
            # Running work remains bounded. Completed work is reusable only for
            # the same frozen component, never a newly expanded event group.
            projected = readback(prior,item_id)
            if prior['status']=='RUNNING' or projected['applicability']['status']=='CURRENT':
                return {'deep_id':prior['deep_id'],'status':prior['status'],'reused':True,
                        'input_changed':projected['input_changed'],'applicability':projected['applicability']}
        deep_id,item=service.begin_deep(item_id)
        launch(service.investigate(deep_id,item))
        return {'deep_id':deep_id,'status':'RUNNING'}

    @app.get('/api/attention/deep/{deep_id}')
    def deep_status(deep_id:str):
        with service.store._connect() as db:
            r=db.execute('SELECT item_id,status,payload,payload_hash FROM attention_deep WHERE deep_id=?',(deep_id,)).fetchone()
        if not r:
            raise HTTPException(404,detail='Attention deep result not found')
        return readback({'deep_id':deep_id,'status':r[1],
                         'result':research.checked(r[2],r[3]) if r[2] else None},r[0])
