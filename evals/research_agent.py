"""Frozen, explicitly synthetic Research Agent product oracle; no external calls."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from market_agent.agent import MockLLMClient
from market_agent.domain import (Account, EvidenceItem, PortfolioContent, PortfolioSnapshot,
                                 Position, PriceObservation, SourceMetadata, portfolio_content_hash)
from market_agent.news import NewsResult
from market_agent.persistence import Store
from market_agent.provider import MarketDataResult, ProviderError
from market_agent.research_task import ResearchAssessment, ResearchIntent
from market_agent.sec import SECSearchResult
from market_agent.tdb import content_hash
from market_agent.thesis import ThesisGenerationMetadata, ThesisProfile, ThesisStatement, ThesisStatus

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
J3 = 'APP 盘前跌到 226，我应该考虑分批加仓吗？'
J5 = '那它下次财报需要重点看什么？'


def item(kind, payload, *, symbol='APP', title='合成资料', timestamp=None):
    digest = content_hash(payload)
    return EvidenceItem(evidence_id='E'+digest[7:23], evidence_type=kind, provider='synthetic-research',
        symbol=symbol, title=title, source_uri='https://example.com/synthetic-research',
        published_at=timestamp or NOW-timedelta(days=1), as_of=timestamp or NOW-timedelta(days=1),
        retrieved_at=NOW-timedelta(minutes=1), content_hash=digest,
        excerpt=payload.get('text'), structured_data=payload)


SUPPORT = item('news', {'text': '合成公司公告：广告客户留存保持韧性，未发现新增客户流失。'}, title='合成 · 广告客户留存更新')
COUNTER = item('sec_filing', {'text': '合成披露：广告归因测量质疑尚未解决，仍需核查对留存与经营现金转换的影响。'}, title='合成 · 归因测量反证')


class Company:
    name = 'synthetic-research'
    def __init__(self, missing=False, stale=False, conflict=False):
        self.missing, self.stale, self.conflict = missing, stale, conflict
        self.calls = []

    def search(self, *args):
        self.calls.append(args)
        symbol = args[0][0] if isinstance(args[0], list) else args[0]
        news = isinstance(args[0], list)
        if self.missing:
            raise ProviderError('configuration', 'synthetic missing company')
        source = SUPPORT if news else COUNTER
        if symbol != 'APP':
            source = item('news' if news else 'sec_filing', {'text': '合成新标的：缺少可判断经营变化的材料。'}, symbol=symbol)
        if self.stale:
            source = item(source.evidence_type, source.structured_data, symbol=symbol, timestamp=NOW-timedelta(days=180))
        if self.conflict and not news:
            source = source.model_copy(update={'evidence_id': SUPPORT.evidence_id})
        return NewsResult(evidence=[source]) if news else SECSearchResult(cik='synthetic', indexed_documents=0, evidence=[source])


class Market:
    name = 'synthetic-research'
    def __init__(self, missing=False, stale=False):
        self.missing, self.stale = missing, stale

    def retrieve_eod(self, symbols, cutoff):
        if self.missing:
            raise ProviderError('configuration', 'synthetic missing price')
        sources, observations = [], []
        for symbol in symbols:
            if symbol != 'APP':
                continue
            stamp = NOW-timedelta(days=30) if self.stale else datetime(2026, 10, 8, 20, tzinfo=timezone.utc)
            source = item('market_data', {'price': '240', 'basis': 'COMPLETED_CLOSE'}, symbol=symbol, timestamp=stamp)
            observations.append(PriceObservation(symbol=symbol, price=Decimal('240'), as_of=stamp,
                provider=source.provider, evidence_id=source.evidence_id))
            sources.append(source)
        return MarketDataResult(observations=observations, evidence=sources)


def setup(folder):
    store = Store(folder / 'research.db')
    if store.get_snapshot():
        return store
    content = PortfolioContent(portfolio_id='synthetic-research', as_of=NOW-timedelta(days=2),
        accounts=(Account(account_id='A', name='合成 Research A', cash=Decimal('12000')),
                  Account(account_id='B', name='合成 Research B', cash=Decimal('3000'))),
        positions=(Position(position_id='A:APP', account_id='A', symbol='APP', quantity=Decimal('40'), avg_cost=Decimal('180')),
                   Position(position_id='B:APP', account_id='B', symbol='APP', quantity=Decimal('10'), avg_cost=Decimal('210'))),
        source=SourceMetadata(kind='manual_entry', imported_at=NOW-timedelta(days=2), source_hash=content_hash({'synthetic': True})))
    snapshot = PortfolioSnapshot(**content.model_dump(), snapshot_id='synthetic-research-snapshot', content_hash=portfolio_content_hash(content))
    store.add_snapshot(snapshot)
    statement = ThesisStatement(text='合成历史 AI 草稿：广告留存是待验证的经营驱动。', evidence_ids=[SUPPORT.evidence_id])
    thesis = ThesisProfile(symbol='APP', thesis_version_id='synthetic-app-draft', as_of=NOW-timedelta(hours=1),
        status=ThesisStatus.AI_DRAFT, generation=ThesisGenerationMetadata(model='synthetic-oracle'),
        core_drivers=[statement], key_assumptions=[statement], execution_risks=[statement],
        invalidation_conditions=[statement], evidence_ids=[SUPPORT.evidence_id])
    evidence = item('thesis', thesis.model_dump(mode='json'), title='合成历史 AI_DRAFT', timestamp=thesis.as_of)
    evidence = evidence.model_copy(update={'source_record_id': thesis.thesis_version_id})
    store.add_thesis(thesis, evidence)
    return store


class OracleLLM(MockLLMClient):
    """Scripted responses measure orchestration/UI, not real semantic quality."""
    model_name = 'synthetic-research-oracle'
    def __init__(self, failure=None, supplemental=False, delay=0):
        self.failure, self.supplemental, self.delay = failure, supplemental, delay
        self.requests = []

    async def complete_structured(self, request):
        from market_agent.llm import LLMError
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure:
            raise LLMError(self.failure, 'synthetic model failure')
        if request.purpose == 'research.intent':
            facts = request.facts
            question = facts['question']
            symbol = (facts['explicit_literals']['symbols'] or [facts['previous_focus']])[0]
            objective = 'EARNINGS_CHECK' if '财报' in question or 'earnings' in question else 'CONSIDER_REDUCE' if '减仓' in question else 'CONSIDER_ADD'
            return ResearchIntent(objective=objective, focus_symbol=symbol, clarification=None,
                sec_query='customer retention attribution measurement operating cash conversion', use_news=True, use_sec=True)
        if request.purpose == 'attention_deep_v1':
            from market_agent.attention import DeepOutput
            # Same frozen Attention contract as the real GPT adapter; never route
            # an Attention packet through ResearchAssessment's sources contract.
            facts = request.facts
            return DeepOutput(what_changed=facts['what_changed'],
                why_it_matters=facts['screen']['rationale'], thesis_impact='UNKNOWN',
                owner_attention='SOON', next_step='REVIEW',
                next_check={'text': '合成深入研究：对照原始披露，核查客户留存与经营现金转换的后续变化。',
                    'evidence_ids': [e['evidence_id'] for e in facts['packet']['evidence']]})
        if request.purpose not in {'research.assess', 'research.reassess'}:
            raise LLMError('configuration', 'Unsupported synthetic request purpose')
        sources = request.facts['sources']
        assertion = next(s['evidence']['evidence_id'] for s in sources if s['identity'] == 'USER_ASSERTION')
        company = [s['evidence']['evidence_id'] for s in sources if s['identity'] == 'VERIFIED_FACT' and s['freshness'] == 'CURRENT' and s['evidence']['evidence_type'] in {'news','sec_filing'}]
        calculation = next((s['evidence']['evidence_id'] for s in sources if s['evidence']['evidence_type'] == 'calculation'), assertion)
        def statement(text, ids=None, identity='AI_DRAFT'):
            return {'text': text, 'identity': identity, 'evidence_ids': ids or [assertion]}
        earnings = request.facts['objective'] == 'EARNINGS_CHECK'
        if not company:
            core = '目前公司证据缺失或过时，不能判断这次下跌是否提供加仓机会；先补查最新经营披露与反证。'
            disposition = 'UNKNOWN'
        elif earnings:
            core = '下次财报优先看广告客户留存、归因测量披露和经营现金转换，检验当前加仓前提能否成立。'
            disposition = 'RESEARCH_ONLY'
        else:
            core = '可以把分批加仓列为条件候选，但先暂缓行动：留存韧性提供支持，归因测量质疑尚未排除，仅凭跌价不足以判断错杀。'
            disposition = 'CONDITIONAL_ADD'
        return ResearchAssessment.model_validate({
            'disposition': disposition, 'core_judgment': statement(core, company or [assertion]),
            'key_reasons': [statement('客户留存与现金转换比单次价格变化更能检验经营驱动。', company or [assertion]),
                statement('已有持仓使追加资金继续增加同一公司的暴露；按所选账户现金与已定价权重评估，尚无确认的买入规模政策。', [calculation])],
            'support': [statement('留存更新为继续调查提供支持，尚不证明归因问题已经解决。', company)] if company else [],
            'counterevidence': [statement('归因测量质疑若传导到客户留存或现金转换，会推翻加仓前提；未获得后续披露时保留这个缺口。', company or [assertion], 'AI_DRAFT' if company else 'UNKNOWN')],
            'action_conditions': [statement('只有在归因披露得到澄清、留存与经营现金转换未受损且账户约束允许时，再考虑分批追加；若经营驱动受损，停止加仓研究。', company or [assertion])],
            'next_checks': [statement('对照下次财报的客户留存、归因测量解释及经营现金转换；把恶化信号与历史 AI 草稿逐项比对。', company or [assertion], 'UNKNOWN')],
            'supplemental_query': 'attribution measurement cash conversion disclosure' if self.supplemental and request.facts['supplemental_allowed'] else None,
        })


def create_oracle_app(folder, *, failure=None, delay=0, llm_client=None):
    # Caller must isolate MIA_DB_PATH before importing main (including subprocesses).
    from market_agent.main import create_app
    store = setup(folder)
    company = Company()
    from market_agent.attention import CompactBrief, ScreenBatch, ScreenOutput
    from market_agent.tdb_contract import EvidenceStatement
    class Screen:
        model_name = 'synthetic-research-attention'
        async def screen(self, packets):
            return ScreenBatch(events=[ScreenOutput(event_key=p.event_key, materiality='MEDIUM',
                direction='MIXED', thesis_impact='UNKNOWN', urgency='SOON', evidence_confidence='MEDIUM',
                rationale=EvidenceStatement(text='合成客户留存更新值得对照归因测量披露。', evidence_ids=[p.evidence[0].evidence_id]),
                brief=CompactBrief(headline=EvidenceStatement(text='合成 · 广告客户留存保持韧性', evidence_ids=[p.evidence[0].evidence_id]),
                    next_check=EvidenceStatement(text='合成研究：核查归因测量披露与经营现金转换。', evidence_ids=[p.evidence[0].evidence_id]))) for p in packets])
    model = llm_client if llm_client is not None else OracleLLM(failure=failure, delay=delay)
    app = create_app(db_path=store.path, market_provider=Market(), llm_client=model,
        news_provider=company, sec_provider=company, attention_screen=Screen(), clock=lambda: NOW)
    return app
