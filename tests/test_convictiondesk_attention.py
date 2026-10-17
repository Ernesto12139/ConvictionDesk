"""Production Today regressions with public-only model input and durable recovery."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

from market_agent.attention import ScreenBatch
from market_agent.domain import EvidenceItem
from market_agent.llm import LLMError
from market_agent.main import create_app
from market_agent.news import NewsResult
from market_agent.provider import ProviderError
from market_agent.research_universe import UniverseChange
from market_agent.tdb import content_hash

NOW = datetime(2026, 10, 17, 4, tzinfo=timezone.utc)


class PublicNews:
    def __init__(self):
        self.fail = False
        self.rows = []
        for index in range(9):
            payload = {'title': f'APP company update {index}', 'text': 'Public operating update.'}
            digest = content_hash(payload)
            self.rows.append(EvidenceItem(evidence_id='E'+digest[7:23], evidence_type='news',
                provider='synthetic-public', symbol='APP', title=payload['title'], excerpt=payload['text'],
                source_record_id=str(index), source_uri=f'https://example.com/public/{index}',
                published_at=NOW-timedelta(minutes=index+1), retrieved_at=NOW, content_hash=digest))

    def search(self, symbols, since, until, limit):
        if self.fail:
            raise ProviderError('timeout', 'synthetic source timeout')
        return NewsResult(evidence=[row for row in self.rows if row.symbol in symbols], coverage_complete=True)


class ScreenModel:
    model_name = 'synthetic-only'

    def __init__(self):
        self.requests = []
        self.fail = False

    async def complete_structured(self, request):
        self.requests.append(request)
        if self.fail:
            raise LLMError('timeout', 'synthetic model timeout')
        events = []
        for packet in request.facts['events']:
            statement = {'text': '该来源未披露新的重大经营变化。',
                'evidence_ids': [packet['evidence'][0]['evidence_id']]}
            events.append({'event_key': packet['event_key'], 'materiality': 'NONE',
                'direction': 'UNKNOWN', 'thesis_impact': 'UNKNOWN', 'urgency': 'ROUTINE',
                'evidence_confidence': 'HIGH', 'rationale': statement,
                'brief': {'headline': statement, 'next_check': statement}})
        return ScreenBatch.model_validate({'events': events})


def service(tmp_path):
    news, model = PublicNews(), ScreenModel()
    app = create_app(tmp_path/'synthetic.sqlite3', market_fixture='evals/fixtures/market_eod.json',
        news_provider=news, llm_client=model, clock=lambda: NOW, environment={})
    attention = app.state.attention_service
    attention.research.change(UniverseChange(symbol='APP', state='WATCHING',
        reason='PRIVATE_NOTE_MUST_STAY_LOCAL'), NOW)
    return attention, news, model


def test_production_small_public_batches_drain_without_false_no_event(tmp_path):
    attention, _, model = service(tmp_path)
    reports = [asyncio.run(attention.scan(*attention.begin())) for _ in range(3)]
    assert max(len(request.facts['events']) for request in model.requests) <= 4
    assert all('PRIVATE_NOTE_MUST_STAY_LOCAL' not in json.dumps(request.facts) for request in model.requests)
    assert all(request.max_output_tokens <= 3072 for request in model.requests)
    assert [row['result_status'] for row in reports] == ['PARTIAL', 'PARTIAL', 'COMPLETED_NO_MATERIAL_EVENTS']
    assert reports[-1]['pending_events'] == 0
    assert sum(row['new_events'] for row in reports) == 9


def test_timeout_retains_exact_receipts_for_manual_retry(tmp_path):
    attention, _, model = service(tmp_path)
    model.fail = True
    failed = asyncio.run(attention.scan(*attention.begin()))
    assert failed['result_status'] == 'PARTIAL' and failed['screened'] == 0
    assert failed['pending_events'] == 9
    failed_ids = model.requests[-1].evidence_ids
    model.fail = False
    recovered = asyncio.run(attention.scan(*attention.begin()))
    assert recovered['screened'] == 4 and recovered['new_events'] == 0
    assert model.requests[-1].evidence_ids == failed_ids


def test_all_sources_unavailable_is_not_no_material_events(tmp_path):
    attention, news, model = service(tmp_path)
    news.fail = True
    report = asyncio.run(attention.scan(*attention.begin()))
    assert report['result_status'] == 'PROVIDER_UNAVAILABLE'
    assert not model.requests


def test_busy_issuer_does_not_starve_other_recent_symbol(tmp_path):
    attention, news, model = service(tmp_path)
    attention.research.change(UniverseChange(symbol='AAPL',state='WATCHING'), NOW)
    news.rows += [row.model_copy(update={'symbol':'AAPL','evidence_id':'EAAPL'+str(index),
        'source_record_id':'AAPL'+str(index), 'title':'AAPL update '+str(index),
        'source_uri':'https://example.com/aapl/'+str(index), 'published_at':NOW-timedelta(seconds=index+1)})
        for index,row in enumerate(news.rows[:4])]
    asyncio.run(attention.scan(*attention.begin()))
    assert {packet['focus_symbol'] for packet in model.requests[0].facts['events']} == {'APP','AAPL'}
