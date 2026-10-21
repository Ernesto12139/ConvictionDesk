"""Replayable event-budget gain with separate-event/revision counterexamples."""
import asyncio
from datetime import timedelta
import pytest

from market_agent.attention import EventPacket
from market_agent.attention_preselection import repost_units, fit_context, MAX_CANDIDATE_BYTES, public_size
from market_agent.domain import EvidenceItem
from market_agent.research_universe import UniverseChange
from market_agent.tdb import content_hash
from test_convictiondesk_attention import NOW, service


HEADLINES=[
    'Orchid halts Riverbend plant production after safety inspection',
    'Orchid plant production halted at Riverbend after safety inspection',
    'Orchid Riverbend plant production on hold after safety inspection',
    'Orchid safety inspection halts production at Riverbend plant',
    'Orchid Riverbend plant halts production following safety inspection',
    'Safety inspection at Orchid Riverbend plant halts production',
]


def article(title,index,symbol='APP',**metadata):
    data={'text':'Frozen synthetic article about Orchid Riverbend plant operations.',
        'published_at_precision':'INSTANT',**metadata}
    return EvidenceItem(evidence_id='Erepost'+str(index),evidence_type='news',provider='synthetic',
        symbol=symbol,title=title,excerpt=data['text'],published_at=NOW-timedelta(hours=2,minutes=index),
        retrieved_at=NOW,source_uri=f'https://example.com/outlet-{index}/article',
        content_hash=content_hash({'title':title,**data}),structured_data=data)


def packets(attention,rows):
    for symbol in {r.symbol for r in rows}:
        attention.research.change(UniverseChange(symbol=symbol,state='WATCHING'),NOW)
        entry=next(e for e in attention.research.entries(NOW) if e.symbol==symbol)
        attention.ingest('frozen-gold',entry,[r for r in rows if r.symbol==symbol],NOW,NOW,complete=False)
    with attention.store._connect() as db:
        return [EventPacket.model_validate(attention.research.checked(*r)) for r in
            db.execute('SELECT payload,payload_hash FROM attention_events ORDER BY rowid')]


@pytest.mark.parametrize('record',[' ','0','None','null','real-id-reused'])
def test_shared_record_id_cannot_erase_conflicting_outlet_urls(tmp_path,record):
    attention,_,_=service(tmp_path)
    first=article(HEADLINES[0],0).model_copy(update={'source_record_id':record})
    second=first.model_copy(update={'evidence_id':'Eother-outlet',
        'source_uri':'https://example.com/another-outlet/actual-article'})
    stored=packets(attention,[first,second])
    assert len(stored)==2
    assert all(p.revision_of is None for p in stored)
    # Even an interleaved placeholder must not obscure the known article URL.
    repeated=second.model_copy(update={'evidence_id':'Eredelivered-other'})
    assert len(packets(attention,[repeated]))==2


def test_cross_outlet_changed_excerpt_is_not_a_revision_of_another_original(tmp_path):
    attention,_,_=service(tmp_path)
    first=article(HEADLINES[0],0)
    second=first.model_copy(update={'evidence_id':'Edifferent-outlet',
        'source_uri':'https://example.com/another-outlet/actual-article','excerpt':'Additional reporting.'})
    stored=packets(attention,[first,second])
    assert len(stored)==2 and all(p.revision_of is None for p in stored)


def test_six_reposts_use_one_event_slot_without_losing_any_original_evidence(tmp_path):
    attention,_,_=service(tmp_path)
    rows=[article(title,i) for i,title in enumerate(HEADLINES)]
    original=packets(attention,rows)
    units,membership,audit=repost_units(original)
    assert len(original)==6 and len(units)==1 and len(membership[units[0].event_key])==6
    assert {e.evidence_id for e in units[0].evidence}=={r.evidence_id for r in rows}
    assert audit['same_round_redundant_slots_avoided']==5 and audit['original_receipts_deleted']==0
    assert audit['qualification']=='UNVERIFIED_CANDIDATE_REPOSTS'


def test_different_lawsuits_quarters_entities_numbers_or_stages_do_not_merge(tmp_path):
    attention,_,_=service(tmp_path)
    titles=[
        'Orchid faces patent lawsuit from Atlas over Riverbend plant design',
        'Orchid faces patent lawsuit from Orion over Riverbend plant design',
        'Orchid reports Q1 2026 Riverbend plant production results',
        'Orchid reports Q2 2026 Riverbend plant production results',
        'Orchid plans Riverbend plant expansion for 300 million dollars',
        'Orchid plans Riverbend plant expansion for 900 million dollars',
        'Orchid in talks to acquire Riverbend plant, sources say',
        'Orchid denies talks to acquire Riverbend plant',
        'Orchid confirms definitive agreement to acquire Riverbend plant',
    ]
    original=packets(attention,[article(t,i) for i,t in enumerate(titles)])
    assert len(repost_units(original)[0])==len(titles)


def test_cross_issuer_unknown_precision_revision_and_metadata_never_collapsed(tmp_path):
    attention,_,_=service(tmp_path)
    rows=[article(HEADLINES[0],0),article(HEADLINES[1],1,symbol='SMR'),
        article(HEADLINES[2],2,event_id='different-case'),
        article(HEADLINES[3],3,event_type='EARNINGS'),
        article(HEADLINES[4],4,new_disclosure=True)]
    original=packets(attention,rows)
    assert len(repost_units(original)[0])==5
    pair=original[:1]+[original[0].model_copy(update={'event_key':'revision','revision_of':original[0].event_key})]
    assert len(repost_units(pair)[0])==2


def test_identical_headline_from_distinct_outlets_keeps_both_original_ids(tmp_path):
    attention,_,_=service(tmp_path)
    rows=[article(HEADLINES[0],0),article(HEADLINES[0],1)]
    original=packets(attention,rows)
    assert len(original)==2,'Weak title equivalence must not erase a distinct original outlet receipt'
    units,_,_=repost_units(original)
    assert len(units)==1 and {e.evidence_id for e in units[0].evidence}=={r.evidence_id for r in rows}


def test_empty_provider_record_id_is_not_strong_original_article_identity(tmp_path):
    attention,_,_=service(tmp_path)
    rows=[article(HEADLINES[0],i).model_copy(update={'source_record_id':''}) for i in range(2)]
    assert len(packets(attention,rows))==2


def test_same_headline_but_new_source_event_identity_is_not_deduplicated(tmp_path):
    attention,_,_=service(tmp_path)
    rows=[article('Orchid files a new lawsuit regarding Riverbend plant operations',0,event_id='case-A'),
        article('Orchid files a new lawsuit regarding Riverbend plant operations',1,event_id='case-B')]
    original=packets(attention,rows)
    assert len(original)==2,'Distinct case identity is new evidence even with an identical headline/summary'
    assert len(repost_units(original)[0])==2


def test_true_same_article_redelivery_does_not_consume_another_slot(tmp_path):
    attention,_,_=service(tmp_path)
    original=article(HEADLINES[0],0)
    repeated=original.model_copy(update={'evidence_id':'Erepeat-new-provider-id'})
    assert len(packets(attention,[original,repeated]))==1


def test_same_article_new_event_metadata_reenters_as_revision_even_if_prose_unchanged(tmp_path):
    attention,_,_=service(tmp_path)
    original=article(HEADLINES[0],0)
    data={**original.structured_data,'new_disclosure':True,'disclosure_stage':'CONFIRMATION'}
    updated=original.model_copy(update={'evidence_id':'Eupdated','structured_data':data,'content_hash':content_hash(data)})
    stored=packets(attention,[original,updated])
    assert len(stored)==2
    revision=next(p for p in stored if p.revision_of)
    assert revision.evidence[0].evidence_id==updated.evidence_id
    assert revision.evidence[1].evidence_id==original.evidence_id
    assert len(repost_units(stored)[0])==2


def test_complete_link_blocks_topic_chain_and_bounds_cluster_and_context(tmp_path):
    attention,_,_=service(tmp_path)
    original=packets(attention,[article(t,i) for i,t in enumerate(HEADLINES)])
    # A source-level unknown precision is never inferred from its timestamp.
    p=original[0];data={**p.evidence[0].structured_data,'published_at_precision':'UNKNOWN'}
    unknown=p.model_copy(update={'event_key':'unknown','evidence':[p.evidence[0].model_copy(update={'structured_data':data})]})
    assert len(repost_units([p,unknown])[0])==2
    units,members,_=repost_units(original)
    fit,deferred=fit_context(units)
    assert not deferred and public_size(fit)<=MAX_CANDIDATE_BYTES
    # Oversized input stays deferred, not truncated, dropped or sent unbounded.
    huge=p.model_copy(update={'evidence':[p.evidence[0].model_copy(update={'excerpt':'x'*20000})]})
    fit,deferred=fit_context([huge,units[0]])
    assert fit==units and deferred==[huge.event_key]


def test_transitive_similarity_cannot_swallow_a_different_topic(tmp_path):
    attention,_,_=service(tmp_path)
    titles=[
        'Orchid Riverbend plant production safety inspection maintenance operations',
        'Orchid Riverbend plant production safety inspection maintenance operations closure review',
        'Orchid Riverbend plant production safety operations closure review restriction',
    ]
    original=packets(attention,[article(t,i) for i,t in enumerate(titles)])
    from market_agent.attention_preselection import signature,compatible
    by_title={p.evidence[0].title:p for p in original}
    a,b,c=[signature(by_title[t]) for t in titles]
    assert compatible(a,b) and compatible(b,c) and not compatible(a,c)
    assert len(repost_units(original)[0])==2


def test_high_overlap_operating_reversal_negation_and_spelled_number_are_separate(tmp_path):
    attention,_,_=service(tmp_path)
    titles=[
        HEADLINES[0],
        'Orchid restarts Riverbend plant production after safety inspection',
        'Orchid does not halt Riverbend plant production after safety inspection',
        'Orchid Riverbend plant cuts three thousand jobs after production safety inspection',
        'Orchid Riverbend plant cuts ten thousand jobs after production safety inspection',
    ]
    assert len(repost_units(packets(attention,[article(t,i) for i,t in enumerate(titles)]))[0])==5


def test_actual_scan_once_per_unit_preserves_payloads_and_retry_and_new_disclosure(tmp_path):
    attention,news,model=service(tmp_path)
    news.rows=[article(title,i) for i,title in enumerate(HEADLINES)]
    model.fail=True
    failed=asyncio.run(attention.scan(*attention.begin()))
    assert failed['screened']==0 and failed['pending_events']==6
    with attention.store._connect() as db:
        frozen=list(db.execute('SELECT event_key,payload,payload_hash FROM attention_events ORDER BY rowid'))
        assert db.execute('SELECT COUNT(*) FROM attention_events WHERE screen IS NULL').fetchone()[0]==6
    model.fail=False
    passed=asyncio.run(attention.scan(*attention.begin(),fetch_sources=False))
    assert passed['screened']==1 and passed['screened_article_receipts']==6
    assert passed['pending_events']==0 and len(model.requests)==2
    assert len(model.requests[-1].facts['events'])==1
    assert {e['evidence_id'] for e in model.requests[-1].facts['events'][0]['evidence']}=={r.evidence_id for r in news.rows}
    assert passed['candidate_selection']['deferred_current']==0
    assert passed['candidate_selection']['clustering']['same_round_redundant_slots_avoided']==5
    with attention.store._connect() as db:
        assert frozen==list(db.execute('SELECT event_key,payload,payload_hash FROM attention_events ORDER BY rowid'))
        assert db.execute('SELECT COUNT(*) FROM attention_events WHERE screen IS NOT NULL').fetchone()[0]==6
    # Do not reuse the earlier verdict to swallow a changed factual disclosure.
    news.rows=[*news.rows,article('Orchid confirms Riverbend plant production resumes after safety inspection',9,new_disclosure=True)]
    new=asyncio.run(attention.scan(*attention.begin()))
    assert new['screened']==1 and new['screened_article_receipts']==1
    assert model.requests[-1].facts['events'][0]['evidence'][0]['evidence_id']=='Erepost9'
    assert len(model.requests)==3  # One finite request per explicitly begun round, including failed first attempt.


def test_material_unit_enters_existing_queue_and_freezes_every_source(tmp_path):
    attention,news,model=service(tmp_path)
    original_complete=model.complete_structured
    async def material(request):
        output=await original_complete(request)
        return output.model_copy(update={'events':[row.model_copy(update={'materiality':'MEDIUM'}) for row in output.events]})
    model.complete_structured=material
    news.rows=[article(title,i) for i,title in enumerate(HEADLINES)]
    report=asyncio.run(attention.scan(*attention.begin()))
    from market_agent.attention_groups import review_units,freeze_row
    units=review_units(attention.research,NOW)
    assert len(units)==1 and report['queued']==1
    frozen=freeze_row(attention.research,units[0])
    assert {e.evidence_id for e in frozen.packet.evidence}=={r.evidence_id for r in news.rows}
    lineage=frozen.screening_lineage['candidate_repost_unit']
    assert len(lineage['member_event_keys'])==6 and lineage['original_receipts_preserved']


@pytest.mark.parametrize('mode',['FAST','DEEP'])
def test_all_cluster_receipts_reach_a_real_scripted_research_task(tmp_path,monkeypatch,mode):
    from evals.v09_intelligence import create_app, NOW as research_now
    from market_agent.news import NewsResult
    from market_agent.research_task import ResearchRequest
    from market_agent.attention_groups import review_units
    # The fixture owns a real Store/ResearchService/Agent with scripted models.
    # All source/network/model paths remain synthetic, as in the offline Harness.
    monkeypatch.setenv('MIA_ISOLATED_ROOT',str(tmp_path))
    app=create_app();scan=app.state.attention_service;research=app.state.research_service
    rows=[]
    for i,title in enumerate(HEADLINES):
        e=article(title,i);data={**e.structured_data,'text':'合成公司披露安全检查后暂停工厂生产。'}
        rows.append(e.model_copy(update={'published_at':research_now-timedelta(hours=1,minutes=i),
            'retrieved_at':research_now,'excerpt':data['text'],'structured_data':data,'content_hash':content_hash({'title':title,**data})}))
    class News:
        def search(self,symbols,*args):return NewsResult(evidence=[e for e in rows if e.symbol in symbols],coverage_complete=True)
    scan.news=News()
    report=asyncio.run(scan.scan(*scan.begin()))
    assert report['screened']==1 and report['screened_article_receipts']==6
    item=review_units(scan.research,research_now)[0]['item']
    t,previous,snapshot=research.begin(ResearchRequest(question='APP 的经营变化影响投资逻辑吗？',
        mode=mode,attention_item_id=item['item_id']))
    asyncio.run(research.execute(t,previous,snapshot))
    saved=research.tasks.get(t.task_id)
    assert saved.status=='COMPLETE',saved.error
    assert {e.evidence_id for e in rows}<={s.evidence.evidence_id for s in saved.sources}
    assert saved.attention_item_id==item['item_id']
    assert all(event.provider!='OPENAI' for event in saved.model_events)
