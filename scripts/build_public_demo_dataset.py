"""Author the v0.8 packaged synthetic replay through the real production services.

Development-only generator. A manual Today scan runs through AttentionService
(provider receipt -> freshness -> dedup -> bounded screening -> ranking), then the
selected item enters real ResearchService Fast and Deep tasks. A scripted oracle
plays the model; financial quarters come from the production SEC fundamentals
code over frozen synthetic XBRL. Every issuer, headline, filing and number is
fictional. Docker serves the resulting rows and never invokes a model.
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from market_agent.attention import ScreenBatch
from market_agent.domain import (Account, EvidenceItem, PortfolioContent, PortfolioSnapshot, Position,
                                 PriceObservation, SourceMetadata, portfolio_content_hash)
from market_agent.main import create_app
from market_agent.news import NewsResult
from market_agent.provider import MarketDataResult, ProviderError
from market_agent.research_task import ResearchAssessment, ResearchIntent, ResearchRequest
from market_agent.research_universe import UniverseChange
from market_agent.sec import SECEDGARProvider, SECSearchResult
from market_agent.tdb import content_hash

# Friday 2026-10-16 11:00 America/New_York. Frozen; never "now".
NOW = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)
CLOSE = datetime(2026, 10, 15, 20, 0, tzinfo=timezone.utc)
HELD, WATCHED = 'XMPL', 'XGRD'
SYNTHETIC_URI = 'https://example.com/convictiondesk-synthetic/'
DESTINATION = Path('src/market_agent/demo_assets/research-replay-v2.json')


def receipt(kind, key, symbol, title, text, published, *, provider='synthetic-public'):
    # Synthetic sources declare an exact instant, like a qualified provider would.
    payload = {'synthetic': True, 'key': key, 'title': title, 'text': text, 'published_at_precision': 'INSTANT'}
    digest = content_hash(payload)
    return EvidenceItem(evidence_id='E'+digest[7:23], evidence_type=kind, provider=provider, symbol=symbol,
        title=title, excerpt=text, source_record_id=key, source_uri=SYNTHETIC_URI+key,
        published_at=published, as_of=published, retrieved_at=NOW-timedelta(minutes=2),
        content_hash=digest, structured_data=payload)


PARTNER = receipt('news', 'xmpl-retail-media', HELD, '虚构 · Example AdCloud 签订多年度零售媒体合作',
    '虚构公司 Example AdCloud 宣布与一家大型零售媒体网络签订多年度合作；同时披露最大客户的续约谈判仍未完成。',
    NOW-timedelta(hours=3))
# Same story re-published under another URL: deduplicated by title/date alias.
PARTNER_REPOST = PARTNER.model_copy(update={'evidence_id': 'E'+content_hash({'repost': 1})[7:23],
    'source_record_id': 'xmpl-retail-media-repost', 'source_uri': SYNTHETIC_URI+'xmpl-retail-media-repost',
    'content_hash': content_hash({'repost': 1}), 'published_at': NOW-timedelta(hours=2, minutes=40)})
BLOG = receipt('news', 'xmpl-blog', HELD, '虚构 · Example AdCloud 产品博客：界面更新说明',
    '虚构公司 Example AdCloud 发布广告管理界面的常规更新说明，未涉及客户、收入或合同。', NOW-timedelta(hours=5))
GRID = receipt('news', 'xgrd-interconnect', WATCHED, '虚构 · Example GridStore 储能项目并网延期',
    '虚构公司 Example GridStore 披露其旗舰储能项目并网时间推迟至下一季度，相关项目融资安排尚未完成。',
    NOW-timedelta(hours=4))
NEWS = {HELD: [PARTNER, PARTNER_REPOST, BLOG], WATCHED: [GRID]}

MDNA = receipt('sec_filing', 'xmpl-10q-mdna', HELD, '虚构 · Example AdCloud 10-Q 管理层讨论',
    '虚构 10-Q 管理层讨论：广告平台收入增长主要来自现有客户支出扩大；管理层未给出最大客户续约的具体结果。',
    datetime(2026, 8, 5, 21, tzinfo=timezone.utc), provider='synthetic-sec')
CONCENTRATION = receipt('sec_filing', 'xmpl-10q-concentration', HELD, '虚构 · Example AdCloud 10-Q 客户集中度附注',
    '虚构 10-Q 附注：最大客户贡献收入约18%，现行合同将于下一季度末到期，续约谈判仍在进行；尚无替代收入的承诺。',
    datetime(2026, 8, 5, 21, tzinfo=timezone.utc), provider='synthetic-sec')
GRID_FILING = receipt('sec_filing', 'xgrd-10q-liquidity', WATCHED, '虚构 · Example GridStore 10-Q 流动性',
    '虚构 10-Q：项目建设期现金消耗持续，管理层表示后续资金来源取决于项目融资完成情况。',
    datetime(2026, 8, 7, 21, tzinfo=timezone.utc), provider='synthetic-sec')

# Synthetic companyfacts. Q4 FY2025 is not reported directly: the production code
# derives it from FY2025 minus the 9M year-to-date figure (same concept and unit).
FILINGS = [('0000000001-25-000003', '10-Q', datetime(2025, 11, 5, 21, tzinfo=timezone.utc)),
           ('0000000001-26-000001', '10-K', datetime(2026, 2, 20, 21, tzinfo=timezone.utc)),
           ('0000000001-26-000002', '10-Q', datetime(2026, 5, 6, 21, tzinfo=timezone.utc)),
           ('0000000001-26-000003', '10-Q', datetime(2026, 8, 5, 21, tzinfo=timezone.utc))]
PERIODS = [  # (accession index, form, start, end, revenue, net income, OCF, capex, SBC, diluted EPS)
    (0, '10-Q', '2025-07-01', '2025-09-30', '412000000', '61000000', '98000000', '7000000', '21000000', '0.58'),
    (0, '10-Q', '2025-01-01', '2025-09-30', '1150000000', '160000000', '262000000', '19000000', '60000000', None),
    (1, '10-K', '2025-01-01', '2025-12-31', '1601000000', '231000000', '371000000', '27000000', '83000000', None),
    (2, '10-Q', '2026-01-01', '2026-03-31', '468000000', '72000000', '115000000', '8000000', '24000000', '0.69'),
    (3, '10-Q', '2026-04-01', '2026-06-30', '497000000', '79000000', '121000000', '9000000', '25000000', '0.75')]
CONCEPTS = {'RevenueFromContractWithCustomerExcludingAssessedTax': 4, 'NetIncomeLoss': 5,
            'NetCashProvidedByUsedInOperatingActivities': 6, 'PaymentsToAcquirePropertyPlantAndEquipment': 7,
            'ShareBasedCompensation': 8}


def companyfacts():
    gaap = {}
    for concept, column in CONCEPTS.items():
        gaap[concept] = {'units': {'USD': [{'start': p[2], 'end': p[3], 'val': int(p[column]),
            'accn': FILINGS[p[0]][0], 'form': p[1]} for p in PERIODS]}}
    gaap['EarningsPerShareDiluted'] = {'units': {'USD/shares': [{'start': p[2], 'end': p[3], 'val': float(p[9]),
        'accn': FILINGS[p[0]][0], 'form': p[1]} for p in PERIODS if p[9]]}}
    gaap['CashAndCashEquivalentsAtCarryingValue'] = {'units': {'USD': [{'end': '2026-06-30', 'val': 640000000,
        'accn': FILINGS[3][0], 'form': '10-Q'}]}}
    return {'facts': {'us-gaap': gaap}}


class SyntheticNews:
    name = 'synthetic-public'

    def search(self, symbols, since, until, limit):
        rows = [e for symbol in symbols for e in NEWS.get(symbol, []) if since <= e.published_at <= until]
        return NewsResult(evidence=sorted(rows, key=lambda e: e.published_at, reverse=True)[:limit],
                          coverage_complete=True)


def offline_transport(request):
    raise AssertionError('synthetic generator must not use the network: '+str(request.url))


class SyntheticSEC(SECEDGARProvider):
    """Production fundamentals/quarterly code; only the HTTP-facing reads are frozen."""
    name = 'synthetic-sec'

    def __init__(self, store):
        super().__init__(store, 'synthetic@example.com', client=httpx.Client(transport=httpx.MockTransport(offline_transport)),
                         clock=lambda: NOW, data_base_url='https://example.com/convictiondesk-synthetic/sec')

    def _resolve_cik(self, symbol):
        if symbol != HELD:
            raise ProviderError('not_found', 'synthetic issuer has no XBRL companyfacts')
        return '0000000001'

    def _recent_filings(self, cik):
        return [{'accession_number': a, 'form': f, 'filed_at': t, 'primary_document': 'synthetic.htm'}
                for a, f, t in FILINGS]

    def _json(self, url):
        return companyfacts()

    def search(self, symbol, query, forms, as_of, limit=4, document_limit=None):
        rows = {HELD: [MDNA], WATCHED: [GRID_FILING]}.get(symbol, [])
        return SECSearchResult(cik='synthetic', indexed_documents=len(rows), evidence=rows[:limit])

    def fast_search(self, symbol, query, forms, as_of, limit=2):
        return self.search(symbol, query, forms, as_of, limit)

    def targeted_search(self, symbol, query, forms, as_of, limit=6):
        rows = [CONCENTRATION] if symbol == HELD and 'customer' in query else []
        return SECSearchResult(cik='synthetic', indexed_documents=0, evidence=rows)


class SyntheticMarket:
    name = 'synthetic-eod'

    def retrieve_eod(self, symbols, cutoff):
        prices = {HELD: '61.40', WATCHED: '18.25'}
        observations, sources = [], []
        for symbol in symbols:
            if symbol in prices and CLOSE <= cutoff:
                source = receipt('market_data', 'close-'+symbol, symbol, '合成收盘价', None, CLOSE,
                                 provider='synthetic-eod').model_copy(update={
                    'structured_data': {'price': prices[symbol], 'basis': 'COMPLETED_CLOSE', 'synthetic': True}})
                observations.append(PriceObservation(symbol=symbol, price=Decimal(prices[symbol]), as_of=CLOSE,
                    provider=source.provider, evidence_id=source.evidence_id))
                sources.append(source)
        return MarketDataResult(observations=observations, evidence=sources)


def statement(text, ids, identity='AI_DRAFT'):
    return {'text': text, 'identity': identity, 'evidence_ids': ids}


def cited(text, ids):
    # Attention statements carry citations only; identity belongs to research memos.
    return {'text': text, 'evidence_ids': ids}


class ScriptedOracle:
    """Plays the model with fixed typed outputs; validators still run on every output."""
    model_name = 'scripted-oracle (synthetic, no model call)'

    def __init__(self):
        self.requests = []

    async def complete_structured(self, request):
        self.requests.append(request.purpose)
        if request.response_model is ScreenBatch:
            return ScreenBatch.model_validate({'events': [self.screen(p) for p in request.facts['events']]})
        if request.response_model is ResearchIntent:
            symbol = request.facts['explicit_literals']['symbols'][0]
            return ResearchIntent(objective='REVIEW_THESIS', focus_symbol=symbol, clarification=None,
                sec_query='customer concentration renewal revenue cash flow', use_news=True, use_sec=True)
        if request.response_model is ResearchAssessment:
            return ResearchAssessment.model_validate(self.assess(request))
        raise AssertionError('unexpected synthetic request '+request.purpose)

    def screen(self, packet):
        evidence = packet['evidence'][0]
        ids = [evidence['evidence_id']]
        if evidence['evidence_id'] == PARTNER.evidence_id:
            return {'event_key': packet['event_key'], 'materiality': 'HIGH', 'direction': 'MIXED',
                'thesis_impact': 'UNKNOWN', 'urgency': 'NOW', 'evidence_confidence': 'MEDIUM',
                'rationale': cited('新合作可能扩大零售媒体收入来源，但最大客户续约尚未完成，两者对经营的影响方向相反。', ids),
                'brief': {'headline': cited('Example AdCloud：新增多年度零售媒体合作，最大客户续约仍未完成', ids),
                          'next_check': cited('核查最大客户的收入占比与合同到期时间。', ids)}}
        if evidence['evidence_id'] == GRID.evidence_id:
            return {'event_key': packet['event_key'], 'materiality': 'HIGH', 'direction': 'NEGATIVE',
                'thesis_impact': 'UNKNOWN', 'urgency': 'SOON', 'evidence_confidence': 'MEDIUM',
                'rationale': cited('旗舰项目并网延期且融资未完成，可能推迟收入确认并增加资金压力。', ids),
                'brief': {'headline': cited('Example GridStore：旗舰储能项目并网延期，融资安排未完成', ids),
                          'next_check': cited('核查项目融资进度与现有现金可支撑的建设时间。', ids)}}
        return {'event_key': packet['event_key'], 'materiality': 'NONE', 'direction': 'NEUTRAL',
            'thesis_impact': 'UNKNOWN', 'urgency': 'ROUTINE', 'evidence_confidence': 'HIGH',
            'rationale': cited('常规产品说明，未披露客户、收入或合同变化。', ids),
            'brief': {'headline': cited('常规产品更新，未见经营变化', ids),
                      'next_check': cited('无需单独核查。', ids)}}

    def assess(self, request):
        facts = request.facts
        symbol = facts['symbol']
        available = {row['evidence']['evidence_id'] for row in facts['sources']}
        if symbol == WATCHED:
            ids = [GRID.evidence_id]
            gap = statement('项目融资是否完成仍未知，现有资料不足以形成方向。', ids, 'UNKNOWN')
            return {'disposition': 'UNKNOWN',
                'core_judgment': statement('方向未形成：并网延期是负面信号，但未持有该标的，且融资结果未知，不足以判断是否值得建立仓位。', ids),
                'key_reasons': [statement('旗舰项目并网推迟至下一季度，收入确认时间可能随之推迟。', ids)],
                'support': [], 'counterevidence': [statement('延期本身未说明项目被取消，融资仍可能完成。', ids)],
                'action_conditions': [statement('若披露项目融资完成且并网时间确定，可重新评估是否建立观察仓位。', ids)],
                'next_checks': [gap],
                'action': {'direction': None, 'status': 'UNDETERMINED', 'evidence_strength': 'INSUFFICIENT',
                    'reason': gap, 'thesis_delta': statement('没有已确认的投资逻辑；本次事件只增加了待核查问题。', ids, 'UNKNOWN')}}
        news = [PARTNER.evidence_id]
        sec = [MDNA.evidence_id] if MDNA.evidence_id in available else news
        reassess = request.purpose == 'research.reassess'
        deep_evidence = [CONCENTRATION.evidence_id] if reassess else []
        data = {'disposition': 'RESEARCH_ONLY',
            'core_judgment': statement('倾向HOLD：新合作支持收入来源扩展，但最大客户续约未完成，现有证据不足以支持继续加仓。', news+sec),
            'key_reasons': [statement('新签多年度零售媒体合作，可能扩大收入来源。', news),
                            statement('10-Q 显示收入增长主要来自现有客户支出扩大。', sec)],
            'support': [statement('合作为多年度安排，不是一次性活动。', news)],
            'counterevidence': [statement('最大客户续约谈判仍未完成，这是不选择ADD的主要原因。', news),
                                statement('合作的收入贡献与时间尚未披露，不足以抵消续约风险；这也是暂不选择REDUCE的原因之一。', news)],
            'action_conditions': [statement('若最大客户完成续约且条款未恶化，可重新评估ADD。', news),
                                  statement('若最大客户不续约且新合作未披露收入贡献，应评估REDUCE。', news)],
            'next_checks': [statement('核查最大客户收入占比与合同到期时间。', news)],
            'action': {'direction': 'HOLD', 'status': 'PROVISIONAL', 'evidence_strength': 'LIMITED',
                'reason': statement('倾向HOLD：支持与反对证据并存，续约结果未知。', news),
                'thesis_delta': statement('未提供已确认的持仓投资逻辑；本次事件新增了一个收入来源和一个待解决的客户集中风险。', news, 'UNKNOWN')}}
        if facts.get('supplemental_allowed'):
            data['evidence_gap'] = {'tool': 'SEC', 'query': 'largest customer concentration renewal',
                'material_question': '最大客户占收入多少？合同何时到期？',
                'why_material': '若集中度高且临近到期，续约失败对收入的影响可能超过新合作的贡献。',
                'expected_information': '取得正式披露中的客户集中度与合同到期信息。', 'evidence_ids': news}
        if reassess:
            both = news+deep_evidence
            data['core_judgment'] = statement('倾向HOLD：补查确认最大客户贡献收入约18%且合同于下一季度末到期；续约尚未失败、新合作已签署，现阶段既不支持ADD，也不足以支持REDUCE。', both)
            data['counterevidence'] = [statement('最大客户贡献收入约18%，合同将于下一季度末到期，续约谈判仍在进行。', deep_evidence, 'VERIFIED_FACT'),
                                       statement('新合作的收入贡献尚未披露，短期内难以替代最大客户的收入占比；这是不选择ADD的主要原因。', both)]
            data['action'] = {'direction': 'HOLD', 'status': 'PROVISIONAL', 'evidence_strength': 'MIXED',
                'reason': statement('倾向HOLD：补查确认客户集中风险有明确期限，但续约尚未失败，也有新增收入来源，尚不足以支持REDUCE。', both),
                'thesis_delta': statement('相比快速判断，客户集中风险从未知变为有正式披露的占比与到期时间；方向不变，风险更具体。', both, 'AI_DRAFT')}
            data['next_checks'] = [statement('跟踪下一季度末之前的续约公告，以及新合作是否披露收入贡献。', both)]
            data['investigation_effect'] = statement('定向补查取得客户集中度附注：最大客户贡献收入约18%，合同于下一季度末到期。风险更具体，但未改变HOLD方向。', deep_evidence)
            data['judgment_change'] = 'UNCHANGED'
        return data


def dump_tables(path):
    from market_agent.public_demo import dump_tables as dump
    with sqlite3.connect(path) as db:
        return dump(db)


async def main():
    with TemporaryDirectory(prefix='convictiondesk-demo-', ignore_cleanup_errors=True) as temp:
        path = Path(temp)/'demo.sqlite3'
        oracle = ScriptedOracle()
        from market_agent.persistence import Store
        store = Store(path)
        content = PortfolioContent(portfolio_id='public-demo-synthetic', as_of=NOW-timedelta(days=1),
            accounts=(Account(account_id='DEMO_USD', name='公开合成账户 · 非真实资产', cash=Decimal('30000')),),
            positions=(Position(position_id='DEMO_USD:'+HELD, account_id='DEMO_USD', symbol=HELD,
                quantity=Decimal('40'), avg_cost=Decimal('52')),),
            source=SourceMetadata(kind='manual_entry', imported_at=NOW-timedelta(days=1),
                source_hash=content_hash({'public_demo': True, 'synthetic': True, 'version': 2})))
        snapshot = PortfolioSnapshot(**content.model_dump(), snapshot_id='public-demo-snapshot-v2',
            content_hash=portfolio_content_hash(content))
        store.add_snapshot(snapshot)
        app = create_app(path, market_provider=SyntheticMarket(), llm_client=oracle, news_provider=SyntheticNews(),
            sec_provider=SyntheticSEC(store), clock=lambda: NOW, environment={})
        attention = app.state.attention_service
        attention.research.change(UniverseChange(symbol=WATCHED, state='WATCHING',
            reason='合成观察标的：项目型公司，用于验证通用性'), NOW-timedelta(hours=6))
        report = await attention.scan(*attention.begin())
        from market_agent.attention_selection import scan_outcome
        outcome = scan_outcome(report)
        assert outcome == 'COMPLETED_WITH_EVENTS', (outcome, report)
        queue = attention.research.queue(NOW, None)
        items = {row['item']['symbol']: row['item'] for row in queue}
        assert set(items) == {HELD, WATCHED}, items.keys()

        service = app.state.research_service

        async def run(item, mode):
            headline = item['screen']['brief']['headline']['text']
            request = ResearchRequest(question=f'{item["symbol"]}：「{headline}」会改变我的研究判断吗？',
                snapshot_id=snapshot.snapshot_id, account_id='DEMO_USD', attention_item_id=item['item_id'], mode=mode)
            task, previous, selected = service.begin(request)
            await service.execute(task, previous, selected)
            saved = service.tasks.get(task.task_id)
            assert saved.status == 'COMPLETE', (saved.status, saved.error, saved.model_events[-1:] )
            return saved

        fast = await run(items[HELD], 'FAST')
        deep = await run(items[HELD], 'DEEP')
        other = await run(items[WATCHED], 'FAST')
        assert fast.action_decision.decision_action == 'HOLD' and fast.investigation['status'] in {'NOT_NEEDED', 'SKIPPED_FAST_BUDGET'}
        assert deep.investigation['status'] == 'NEW_EVIDENCE'
        assert other.action_decision.decision_status == 'UNDETERMINED' and other.action_decision.decision_action is None
        examples = [
            {'label': 'Today → 快速判断 · 持仓标的', 'symbol': HELD, 'mode': 'FAST', 'task_id': fast.task_id,
             'revision': fast.revision, 'attention_item_id': items[HELD]['item_id']},
            {'label': 'Today → 深入研究 · 定向补查', 'symbol': HELD, 'mode': 'DEEP', 'task_id': deep.task_id,
             'revision': deep.revision, 'attention_item_id': items[HELD]['item_id']},
            {'label': 'Today → 快速判断 · 未持有项目型公司', 'symbol': WATCHED, 'mode': 'FAST', 'task_id': other.task_id,
             'revision': other.revision, 'attention_item_id': items[WATCHED]['item_id']}]
        data = {'cutoff': NOW.isoformat(), 'scan_outcome': outcome, 'examples': examples,
                'market': SyntheticMarket().retrieve_eod([HELD, WATCHED], NOW).model_dump(mode='json'),
                'model_requests': {'scripted_oracle': len(oracle.requests), 'paid_model_requests': 0},
                'tables': dump_tables(path)}
    payload = {'version': 2, 'synthetic': True, 'execution': 'OFFLINE_REPLAY',
               'generator': 'scripted-oracle / real AttentionService + ResearchService',
               'content_hash': content_hash(data), 'data': data}
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_bytes((json.dumps(payload, ensure_ascii=False, indent=1)+'\n').encode('utf-8'))
    print(json.dumps({'synthetic': True, 'scan_outcome': outcome, 'tables': sorted(data['tables']),
                      'oracle_requests': len(oracle.requests), 'dataset_hash': payload['content_hash']}))


if __name__ == '__main__':
    asyncio.run(main())
