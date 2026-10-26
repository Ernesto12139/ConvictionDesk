"""Pre-submit research route preview: what a question will trigger, before any cost.

Deterministic and local. Explicit symbols, prices and share counts come from the
same extract_literals() the research run uses. The objective is a keyword
pre-judgement shown as such; the research model still classifies the question
and nothing here is saved, sent or used as evidence.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from .llm import LLMError
from .research_task import extract_literals

OBJECTIVES = (
    ('CONSIDER_REDUCE', r'减仓|卖出|清仓|止损|止盈|卖掉|减持|trim|sell|exit|reduce'),
    ('CONSIDER_ADD', r'加仓|买入|建仓|补仓|抄底|增持|逢低|\badd\b|\bbuy\b'),
    ('EARNINGS_CHECK', r'财报|业绩|季报|年报|指引|earnings|guidance'),
    ('EXPLAIN_CHANGE', r'为什么|为何|原因|大跌|大涨|暴跌|暴涨|下跌|上涨|why'),
    ('REVIEW_THESIS', r'逻辑|持有|还能拿|thesis|长期|基本面|持仓判断|改变.{0,12}判断'),
)
LABELS = {'CONSIDER_ADD': '是否加仓', 'CONSIDER_REDUCE': '是否减仓 / 退出', 'EARNINGS_CHECK': '财报研究',
          'EXPLAIN_CHANGE': '解释变化', 'REVIEW_THESIS': '复核持仓逻辑', 'UNSUPPORTED': '一般研究问题'}
# Quoted headlines are someone else's words: they never set the objective.
QUOTED = re.compile(r'「[^」]*」|“[^”]*”|"[^"]*"')
SCOPE_LABELS = {'HOLDING': '持仓', 'WATCHING': '持续跟踪', 'CANDIDATE': '待评估', 'ARCHIVED': '已暂停'}


class RoutePreviewRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1200)


def extract_symbols(text):
    try:
        return extract_literals(text)['symbols']
    except LLMError:
        return []


def preview(question, entries, *, jev_route='OFF', preferences=None):
    try:
        literals = extract_literals(question)
        literal_error = None
    except LLMError as error:
        literals, literal_error = {'symbols': [], 'prices': [], 'quantities': []}, error.code
    known = {e.symbol: e.state for e in entries}
    own_words = QUOTED.sub(' ', question)
    quoted_only = set(literals['symbols']) - set(extract_symbols(own_words))
    symbols = [{'symbol': s, 'scope': known.get(s, 'OUTSIDE_UNIVERSE'), 'in_quote': s in quoted_only,
                'label': ('引文中的代码' if s in quoted_only and s not in known
                          else SCOPE_LABELS.get(known.get(s), '不在研究范围'))} for s in literals['symbols'][:5]]
    objective = next((key for key, pattern in OBJECTIVES if re.search(pattern, own_words, re.I)), 'UNSUPPORTED')
    decision = objective in {'CONSIDER_ADD', 'CONSIDER_REDUCE'}
    focus = [s for s in symbols if not s['in_quote']]
    deep = decision or objective == 'EARNINGS_CHECK' or len(focus) > 1
    holding = any(s['scope'] == 'HOLDING' for s in symbols)
    tools = [{'tool': 'portfolio', 'label': '持仓与现金快照', 'when': '始终（所选账户）'},
             {'tool': 'news', 'label': '近期公司新闻', 'when': '有股票代码时'},
             {'tool': 'sec', 'label': 'SEC 文件检索', 'when': '深度研究或需要一手披露时'},
             {'tool': 'quote', 'label': '价格参考（EOD / 最新报价）', 'when': '有股票代码时'}]
    if decision:
        tools.append({'tool': 'action', 'label': '证据约束的 ADD / HOLD / REDUCE / EXIT 判断', 'when': '加减仓问题'})
    if literals['prices'] or literals['quantities']:
        tools.append({'tool': 'whatif', 'label': '单账户调仓预演（结果页）', 'when': '问题包含价格或股数'})
    return {
        'symbols': symbols, 'prices': literals['prices'], 'quantities': literals['quantities'],
        'literal_error': literal_error,
        'objective': {'key': objective, 'label': LABELS[objective], 'basis': 'KEYWORD_PREJUDGEMENT_MODEL_CONFIRMS'},
        'suggested_mode': 'DEEP' if deep else 'FAST',
        'mode_reason': ('加减仓判断需要完整证据与反证' if decision else '财报研究需要一手披露' if objective == 'EARNINGS_CHECK'
                        else '涉及多个标的' if len(focus) > 1 else '单一问题，快速研究即可回答'),
        'holding_context': holding,
        'tools': tools,
        'jev': {'route': jev_route, 'role': '意图、工具与研究深度路由；可提出受约束的方向候选',
                'effect': {'OFF': 'DISABLED', 'SHADOW': 'RECORDED_ONLY', 'COMPARE': 'COMPARED_WITH_VALIDATED_RESULT',
                           'FAST_CANDIDATE': 'CANDIDATE_REVIEWED_BY_VALIDATORS'}.get(jev_route, 'DISABLED')},
        'model': preferences or {},
        'saved': False, 'model_calls': 0,
    }


def register_route_preview(app, universe, clock, *, model_profiles=None, jev_runtime=None):
    """Reads the Provider Hub's persisted Fast/Deep defaults and Jev mode when enabled."""
    @app.post('/api/research/route-preview')
    def route_preview(body: RoutePreviewRequest):
        defaults = getattr(model_profiles, 'defaults', None) or {}
        return preview(body.question, universe(clock()),
                       jev_route=jev_runtime.mode if jev_runtime else 'OFF',
                       preferences={mode.lower(): profile for mode, profile in defaults.items()})
