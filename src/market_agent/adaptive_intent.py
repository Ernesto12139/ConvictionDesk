"""Typed intent resolution for one investor question, without side effects.

The deterministic parser owns every literal. A bounded `SemanticDraft` may only
fill a gap the parser left open; it can never introduce a price, quantity,
symbol, CIK or account that the question did not contain, and it can never
authorize an order. This module performs no I/O, no network call and no model
call; callers remain responsible for `ResearchService` orchestration.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .domain import FrozenModel, utc
from .provider import SYMBOL
from .research_task import (NON_TICKER_ACRONYMS, Objective, SEC_FORM_LITERAL,
                            TRADE_ACTION, WORD_QUANTITY, extract_literals)

Goal = Literal['INVESTIGATE', 'PRIORITIZE', 'COMPARE', 'HYPOTHETICAL', 'FOLLOW_UP',
               'UNSUPPORTED_TRADE', 'UNKNOWN']
EvidenceKind = Literal['PORTFOLIO', 'THESIS', 'PRICE', 'COMPANY_EVENT', 'SEC_FILING',
                       'FUNDAMENTALS', 'PRIOR_DECISION', 'PEER_OR_MARKET']
AccountCoverage = Literal['SINGLE_ACCOUNT', 'PORTFOLIO', 'ALL_ACCOUNTS', 'UNKNOWN']
PremiseKind = Literal['CURRENT_FACT', 'USER_ASSERTION', 'HYPOTHETICAL_PRICE']
ClarificationCode = Literal['MISSING_SYMBOL', 'MULTIPLE_SYMBOLS', 'MISSING_GOAL',
                            'TRADE_NOT_SUPPORTED', 'SCOPE_AMBIGUOUS', 'UNSUPPORTED_QUESTION']

HYPOTHETICAL_MARKER = re.compile(r'如果|假如|假设|若是|要是|倘若|what\s+if|\bif\b', re.I)
PRICE_MOVE = re.compile(r'跌到|跌至|涨到|涨至|回落到|回撤到|跌破|突破|'
                        r'breaks?\s+below|drops?\s+to|falls?\s+to|rises?\s+to', re.I)
ADD_ADVICE = re.compile(r'抄底|加仓|补仓|建仓|上车|买点|买入|买(?!卖)|'
                        r'\bbuy(?:ing)?\b|\badd(?:ing)?\s+to\b', re.I)
REDUCE_ADVICE = re.compile(r'减仓|止盈|清仓|离场|卖出|sell|reduce|trim|exit', re.I)
ADVICE_MARKER = re.compile(f'{ADD_ADVICE.pattern}|{REDUCE_ADVICE.pattern}', re.I)
# An order is requested even when the sentence is polite or interrogative.
ORDER_REQUEST = re.compile(
    r'帮我(?:下单|挂单|委托)|帮我(?:买入|卖出|加仓|减仓|清仓|建仓)|'
    r'下单(?:买入|卖出|加仓|减仓|清仓|建仓)|挂单|委托(?:买|卖)|'
    r'\b(?:place|submit|execute)\s+(?:an?\s+|the\s+)?(?:order|trade)\b|'
    r'\bmarket\s+order\b', re.I)
# An instruction to execute an order, as opposed to advice sought under a condition.
ORDER_IMPERATIVE = re.compile(
    r'市价单|限价单|执行(?:订单|交易|指令)|'
    r'立刻(?:买入|卖出|加仓|减仓|清仓|建仓)|马上(?:买入|卖出)|立即(?:买入|卖出)|'
    r'\b(?:just|now|immediately)\s+(?:sell|buy|exit|reduce|trim|liquidate)\b|'
    r'\b(?:sell|buy|exit|liquidate)\s+(?:my\s+)?(?:whole|entire|full)\s+'
    r'(?:position|holding|stake)\b', re.I)
# Advice phrasing wins over an imperative-looking trade verb inside a question.
QUESTION_SHAPE = re.compile(r'吗|呢|[?？]|是否|要不要|能不能|可不可以|该不该|'
                            r'\b(?:should|could|would|can|shall|do)\s+(?:i|we)\b|'
                            r'\bwhat\s+if\b', re.I)
PRIORITIZE_MARKER = re.compile(r'最值得|优先级|优先复核|先复核|先看|哪个持仓|哪只|'
                               r'最需要|最该|复核哪|排序|rank|prioriti[sz]e|'
                               r'which\s+(?:holding|position)', re.I)
COMPARE_MARKER = re.compile(r'对比|比较|相比|哪个更好|孰优|\bvs\.?\b|\bversus\b|compare', re.I)
THESIS_MARKER = re.compile(r'持仓逻辑|投资逻辑|thesis|论点|逻辑', re.I)
EARNINGS_MARKER = re.compile(r'财报|业绩|季报|年报|earnings|guidance|quarter', re.I)
FINANCIAL_FACT_MARKER = re.compile(
    r'收入|营收|经营现金流|现金流量|\brevenue\b|\boperating\s+cash\s+flow\b|\bOCF\b', re.I)
REVIEW_MARKER = re.compile(r'复核|复盘|重新评估|review', re.I)
FOLLOW_UP_MARKER = re.compile(r'那它|那么它|它呢|这只|该股|这个标的|接着|继续|下一步|'
                              r'再查|follow\s*-?\s*up|what\s+about', re.I)
FACT_CHECK_MARKER = re.compile(r'核查|核实|哪些事实|哪些数据|需要(?:确认|验证)|'
                               r'check\b|verify\b|what\s+facts', re.I)
CIK_LITERAL = re.compile(r'CIK\s*[:#]?\s*(\d{10})', re.I)
DROP_OR_RISE = re.compile(r'跌|回撤|回落|drops?|falls?|below|support', re.I)


class IntentError(ValueError):
    """Typed refusal; callers surface the code instead of guessing intent."""

    def __init__(self, code: str, message: str):
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message


def _norm_symbol(value: str) -> str:
    value = value.removeprefix('$').upper()
    if not SYMBOL.fullmatch(value):
        raise ValueError('invalid symbol')
    return value


class ExplicitLiterals(FrozenModel):
    """Exactly what the deterministic parser found; nothing inferred."""

    symbols: tuple[str, ...] = ()
    prices: tuple[Decimal, ...] = ()
    quantities: tuple[Decimal, ...] = ()
    ciks: tuple[str, ...] = ()

    @property
    def user_price(self) -> Decimal | None:
        return self.prices[0] if len(self.prices) == 1 else None


class EntityRef(FrozenModel):
    symbol: str | None = None
    issuer_name: str | None = None
    cik: str | None = None
    resolution: Literal['EXPLICIT', 'INHERITED', 'UNRESOLVED'] = 'EXPLICIT'

    _symbol = field_validator('symbol')(
        lambda value: None if value is None else _norm_symbol(value))

    @field_validator('cik')
    @classmethod
    def _cik(cls, value):
        if value is None:
            return None
        if not re.fullmatch(r'\d{10}', value):
            raise ValueError('CIK must be ten digits')
        return value

    @model_validator(mode='after')
    def _identified(self):
        if self.symbol is None and self.cik is None and not self.issuer_name:
            raise ValueError('entity needs a symbol, CIK or issuer name')
        if self.resolution != 'UNRESOLVED' and self.symbol is None and self.cik is None:
            raise ValueError('resolved entity needs a symbol or CIK')
        return self


class AccountScope(FrozenModel):
    account_ids: tuple[str, ...] = ()
    coverage: AccountCoverage = 'UNKNOWN'
    source: Literal['EXPLICIT', 'INHERITED', 'DEFAULT_PORTFOLIO', 'UNRESOLVED'] = 'UNRESOLVED'

    @field_validator('account_ids')
    @classmethod
    def _ids(cls, value):
        cleaned = tuple(sorted({item.strip() for item in value}))
        if any(not item for item in cleaned):
            raise ValueError('blank account id')
        return cleaned

    @model_validator(mode='after')
    def _coverage_matches(self):
        if self.coverage == 'SINGLE_ACCOUNT' and len(self.account_ids) != 1:
            raise ValueError('single-account scope needs exactly one account')
        if self.coverage == 'UNKNOWN' and self.account_ids:
            raise ValueError('unknown coverage cannot name accounts')
        return self


class Premise(FrozenModel):
    """A starting belief, never a market fact unless verified from evidence."""

    premise_id: str = Field(min_length=1, max_length=80)
    kind: PremiseKind
    text: str = Field(min_length=1, max_length=400)
    value: Decimal | None = None
    symbol: str | None = None
    verified: bool = False
    origin_question: str | None = Field(default=None, max_length=1200)
    asserted_at: datetime | None = None

    _symbol = field_validator('symbol')(
        lambda value: None if value is None else _norm_symbol(value))
    _asserted = field_validator('asserted_at')(lambda value: None if value is None else utc(value))

    @field_validator('value')
    @classmethod
    def _positive(cls, value):
        if value is None:
            return None
        if not value.is_finite() or value <= 0:
            raise ValueError('premise value must be finite and positive')
        return value

    @model_validator(mode='after')
    def _provenance(self):
        if self.kind != 'CURRENT_FACT' and self.verified:
            raise ValueError('only a current fact can be verified')
        if self.kind != 'CURRENT_FACT' and self.value is None:
            raise ValueError('user/hypothetical premise needs its literal value')
        if self.kind == 'USER_ASSERTION' and not self.origin_question:
            raise ValueError('user assertion needs its original question')
        return self


class NecessaryCondition(FrozenModel):
    condition_id: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=6, max_length=300)
    evidence_kind: EvidenceKind
    required: bool = True
    historical: bool = False
    rationale: str = Field(min_length=6, max_length=300)


class Clarification(FrozenModel):
    code: ClarificationCode
    question: str = Field(min_length=4, max_length=300)
    blocking: bool = True


class PriorTaskSummary(FrozenModel):
    """The minimum a follow-up may inherit; supplied by the caller, not memory IO."""

    task_id: str = Field(min_length=1, max_length=80)
    thread_root_id: str | None = Field(default=None, max_length=80)
    goal: Goal = 'INVESTIGATE'
    objective: Objective = 'UNSUPPORTED'
    symbol: str | None = None
    account_ids: tuple[str, ...] = ()
    cutoff: datetime | None = None

    _symbol = field_validator('symbol')(
        lambda value: None if value is None else _norm_symbol(value))
    _cutoff = field_validator('cutoff')(lambda value: None if value is None else utc(value))


class ResolvedContext(FrozenModel):
    task_id: str = Field(min_length=1, max_length=80)
    parent_task_id: str | None = Field(default=None, max_length=80)
    thread_root_id: str | None = Field(default=None, max_length=80)
    symbol: str | None = None
    goal: Goal = 'UNKNOWN'
    account_scope: AccountScope
    inherited_fields: tuple[str, ...] = ()
    invalidated_fields: tuple[str, ...] = ()
    memory_notes: tuple[str, ...] = ()

    @field_validator('symbol')
    @classmethod
    def _symbol(cls, value):
        return None if value is None else _norm_symbol(value)


class SemanticDraft(FrozenModel):
    """Optional model output. It has no numeric field by construction."""

    goal: Goal | None = None
    focus_symbol: str | None = None
    issuer_name: str | None = Field(default=None, max_length=120)
    sec_query: str | None = Field(default=None, max_length=240)
    use_news: bool | None = None
    use_sec: bool | None = None
    extra_conditions: tuple[str, ...] = ()

    @field_validator('focus_symbol')
    @classmethod
    def _symbol(cls, value):
        return None if value is None else _norm_symbol(value)


class AdaptiveIntent(FrozenModel):
    version: Literal['adaptive-intent-v1'] = 'adaptive-intent-v1'
    question: str = Field(min_length=1, max_length=1200)
    goal: Goal
    objective: Objective
    entities: tuple[EntityRef, ...] = ()
    primary_symbol: str | None = None
    account_scope: AccountScope
    premises: tuple[Premise, ...] = ()
    conditions: tuple[NecessaryCondition, ...] = ()
    clarification: Clarification | None = None
    resolved: ResolvedContext
    literals: ExplicitLiterals = ExplicitLiterals()
    trade_refusal: bool = False
    asks_for_fact_checks: bool = False
    use_news: bool = True
    use_sec: bool = True
    sec_query: str = Field(min_length=1, max_length=240)

    @field_validator('primary_symbol')
    @classmethod
    def _symbol(cls, value):
        return None if value is None else _norm_symbol(value)

    @property
    def clarification_required(self) -> bool:
        return bool(self.clarification and self.clarification.blocking)

    @property
    def user_price(self) -> Decimal | None:
        """The hypothetical/asserted price; never a market observation."""

        return next((p.value for p in self.premises if p.value is not None), None)

    @property
    def user_price_premise(self) -> Premise | None:
        return next((p for p in self.premises if p.value is not None), None)

    def evidence_kinds(self, *, required_only=False) -> tuple[EvidenceKind, ...]:
        return tuple(dict.fromkeys(c.evidence_kind for c in self.conditions
                                   if c.required or not required_only))

    def to_research_intent(self):
        """Adapter onto the existing, unmodified ResearchService contract."""

        from .research_task import ResearchIntent
        return ResearchIntent(
            objective=self.objective,
            focus_symbol=self.primary_symbol,
            clarification=(self.clarification.question
                           if self.clarification and self.clarification.blocking else None),
            sec_query=self.sec_query,
            use_news=self.use_news,
            use_sec=self.use_sec)

    def snapshot_payload(self) -> dict:
        """JSON-safe review payload; deliberately carries no portfolio numbers."""

        return {
            'version': self.version,
            'goal': self.goal,
            'objective': self.objective,
            'primary_symbol': self.primary_symbol,
            'entities': [e.model_dump(mode='json') for e in self.entities],
            'account_scope': self.account_scope.model_dump(mode='json'),
            'premises': [{'kind': p.kind, 'text': p.text,
                          'value': None if p.value is None else format(p.value, 'f'),
                          'verified': p.verified, 'symbol': p.symbol}
                         for p in self.premises],
            'conditions': [c.model_dump(mode='json') for c in self.conditions],
            'clarification': (self.clarification.model_dump(mode='json')
                              if self.clarification else None),
            'trade_refusal': self.trade_refusal,
            'asks_for_fact_checks': self.asks_for_fact_checks,
            'resolved': self.resolved.model_dump(mode='json'),
        }


def _conditions(goal: Goal, *, has_price: bool, asks_checks: bool,
                earnings: bool, financials: bool = False) -> tuple[NecessaryCondition, ...]:
    rows: list[NecessaryCondition] = []
    if goal == 'UNSUPPORTED_TRADE':
        return ()

    def add(condition_id, question, kind, rationale, required=True, historical=False):
        rows.append(NecessaryCondition(condition_id=condition_id, question=question,
                                       evidence_kind=kind, required=required,
                                       historical=historical, rationale=rationale))

    if goal in {'INVESTIGATE', 'HYPOTHETICAL', 'FOLLOW_UP'}:
        add('portfolio-context', 'What is the current holding, cost and account coverage?',
            'PORTFOLIO', 'A position-specific judgment needs the real position, not an assumed one.')
        add('prior-thesis', 'What did the prior thesis claim, and with which provenance?',
            'THESIS', 'A change can only be assessed against a recorded thesis.', historical=True)
        add('company-events', 'Which recent company events bear on the thesis?',
            'COMPANY_EVENT', 'The question is about recent change, so recent events are load-bearing.')
        add('primary-filing', 'Does a primary filing confirm the reported change?',
            'SEC_FILING', 'Unverified commentary is not a disclosure; the primary text is required.')
    if goal == 'HYPOTHETICAL' or has_price:
        add('qualified-price', 'Is there a current, rights-qualified market price?',
            'PRICE', 'A hypothetical entry price cannot be compared to an unqualified quote.')
    if goal == 'HYPOTHETICAL':
        add('valuation-basis', 'What deterministic value and weight does the scenario imply?',
            'PORTFOLIO', 'Sizing commentary needs exact decimal arithmetic on the snapshot.')
    if (goal == 'INVESTIGATE' and not has_price or financials and
            goal in {'INVESTIGATE', 'FOLLOW_UP', 'HYPOTHETICAL', 'COMPARE'}):
        add('fundamentals', 'Do exact-period fundamentals support the thesis drivers?',
            'FUNDAMENTALS', 'Quantitative claims need exact-period receipts.',
            required=financials)
    if goal == 'PRIORITIZE':
        add('portfolio-context', 'Which holdings exist, with what sizes and coverage?',
            'PORTFOLIO', 'Prioritization is impossible without the real portfolio.')
        add('holding-events', 'Which holdings have the newest material events?',
            'COMPANY_EVENT', 'Recency of change is the primary prioritization signal.')
        add('thesis-gaps', 'Which holding theses are unreviewed or contradicted?',
            'THESIS', 'An unreviewed thesis is a stronger review candidate.')
        add('prior-decisions', 'What was decided last time for the leading candidate?',
            'PRIOR_DECISION', 'Re-review should acknowledge the prior decision.',
            required=False)
    if goal == 'COMPARE':
        add('comparison-facts', 'Which comparable, exact-period facts exist for each option?',
            'FUNDAMENTALS', 'A comparison needs the same fact on the same basis.')
        add('company-events', 'Which recent events differ across the options?',
            'COMPANY_EVENT', 'A comparison on stale events can invert the answer.')
        add('thesis-gaps', 'Which thesis claims are actually evidenced for each option?',
            'THESIS', 'Unevidenced claims must not decide a comparison.')
    if goal == 'FOLLOW_UP' and earnings:
        add('earnings-period', 'Which exact period does the next report cover?',
            'FUNDAMENTALS', 'An earnings check needs the exact period, not a general outlook.')
        add('guidance-baseline', 'What did management previously guide, verbatim?',
            'SEC_FILING', 'Guidance surprises require the prior primary statement.')
    if asks_checks and not rows:
        add('fact-inventory', 'Which facts must be checked before any judgment?',
            'COMPANY_EVENT', 'The question explicitly asks for the fact inventory.')
    return tuple(rows)


def _objective(goal: Goal, question: str, prior: PriorTaskSummary | None) -> Objective:
    if goal == 'UNSUPPORTED_TRADE':
        return 'UNSUPPORTED'
    if goal == 'PRIORITIZE':
        return 'REVIEW_THESIS'
    if goal == 'COMPARE':
        return 'REVIEW_THESIS'
    if EARNINGS_MARKER.search(question):
        return 'EARNINGS_CHECK'
    if goal == 'HYPOTHETICAL':
        if REDUCE_ADVICE.search(question):
            return 'CONSIDER_REDUCE'
        if ADD_ADVICE.search(question):
            return 'CONSIDER_ADD'
        return 'CONSIDER_REDUCE' if re.search(r'涨到|涨至|rises?\s+to', question) else 'CONSIDER_ADD'
    if goal == 'FOLLOW_UP' and prior is not None:
        return prior.objective if prior.objective != 'UNSUPPORTED' else 'REVIEW_THESIS'
    if REVIEW_MARKER.search(question) or THESIS_MARKER.search(question):
        return 'REVIEW_THESIS'
    return 'EXPLAIN_CHANGE'


def _sec_query(goal: Goal, objective: Objective, earnings: bool) -> str:
    if earnings:
        return 'earnings guidance outlook segment results'
    return {
        'HYPOTHETICAL': 'recent company events capital allocation risk',
        'PRIORITIZE': 'recent material events thesis change',
        'COMPARE': 'segment results capital allocation risk',
        'FOLLOW_UP': 'earnings guidance outlook',
        'UNSUPPORTED_TRADE': 'recent company events',
        'UNKNOWN': 'recent company events',
    }.get(goal, 'recent company events thesis drivers disclosure')


def _build_scope(account_scope: AccountScope | None, prior: PriorTaskSummary | None) -> AccountScope:
    if account_scope is not None:
        return account_scope
    if prior is not None and prior.account_ids:
        return AccountScope(account_ids=prior.account_ids,
                            coverage='SINGLE_ACCOUNT' if len(prior.account_ids) == 1 else 'PORTFOLIO',
                            source='INHERITED')
    return AccountScope(source='UNRESOLVED')


def _scope_signature(account_ids, coverage: AccountCoverage | None = None):
    """Coverage mode plus exact ids; an empty id set is an aggregate scope, not a wildcard."""

    ids = tuple(sorted(account_ids))
    if coverage == 'ALL_ACCOUNTS':
        return ('ALL_ACCOUNTS', ())
    if coverage == 'UNKNOWN':
        return ('UNKNOWN', ids)
    if not ids:
        return ('ALL_ACCOUNTS', ())
    return ('SINGLE_ACCOUNT' if len(ids) == 1 else 'PORTFOLIO', ids)


def _build_entities(*, symbols, ciks, prior, follow_up):
    entities = [EntityRef(symbol=s, resolution='EXPLICIT') for s in symbols]
    entities += [EntityRef(cik=c, resolution='EXPLICIT') for c in ciks]
    inherited = False
    if not entities and prior is not None and prior.symbol and follow_up:
        entities = [EntityRef(symbol=prior.symbol, resolution='INHERITED')]
        inherited = True
    return tuple(entities), inherited


def _premise_text(question: str, price: Decimal) -> str:
    literal = format(price, 'f')
    at = question.find(literal)
    if at < 0:
        return question
    start = max(0, at - 24)
    end = min(len(question), at + len(literal) + 8)
    return question[start:end].strip(' ，。；;!?！？')


def _premises(question, literals, primary_symbol, *, hypothetical, asserted_at):
    if not literals.prices:
        return ()
    rows = []
    for index, price in enumerate(literals.prices):
        kind: PremiseKind = 'HYPOTHETICAL_PRICE' if hypothetical else 'USER_ASSERTION'
        rows.append(Premise(
            premise_id=f'question-price-{index + 1}', kind=kind,
            text=_premise_text(question, price),
            value=price, symbol=primary_symbol, verified=False,
            origin_question=question, asserted_at=asserted_at))
    return tuple(rows)


def parse_intent(question: str, *, task_id: str, account_scope: AccountScope | None = None,
                 prior: PriorTaskSummary | None = None, semantic: SemanticDraft | None = None,
                 asserted_at: datetime | None = None,
                 explicit_goal: Goal | None = None) -> AdaptiveIntent:
    """Resolve one question deterministically, then optionally merge a draft."""

    if not question.strip():
        raise IntentError('empty_question', 'question is blank')
    try:
        literals = extract_literals(question)
    except Exception as exc:  # LLMError carries the literal refusal code
        raise IntentError(getattr(exc, 'code', 'invalid_literal'), str(exc)) from exc

    symbols = [s for s in literals['symbols']
               if s not in NON_TICKER_ACRONYMS | {'CIK'}
               and not any(s in match.group() and match.group() != s
                           for match in SEC_FORM_LITERAL.finditer(question))]
    ciks = list(dict.fromkeys(CIK_LITERAL.findall(question)))
    prices = tuple(Decimal(p) for p in literals['prices'])
    quantities = tuple(Decimal(q) for q in literals['quantities'])
    explicit = ExplicitLiterals(symbols=tuple(symbols), prices=prices,
                                quantities=quantities, ciks=tuple(ciks))

    price_scenario = bool(explicit.prices or PRICE_MOVE.search(question))
    quantity_scenario = bool(explicit.quantities or WORD_QUANTITY.search(question))
    # "假设我买入100股…" previews an order under a condition; it is not an
    # instruction to place that order.
    hypothetical = bool(
        (HYPOTHETICAL_MARKER.search(question) and (price_scenario or quantity_scenario))
        or (ADVICE_MARKER.search(question) and PRICE_MOVE.search(question)))
    # A question shape ("…吗？", "should I…") keeps advice phrasing in research; an
    # execution directive outranks the conditional framing around it.
    order_directive = bool(ORDER_REQUEST.search(question)
                           or (ORDER_IMPERATIVE.search(question)
                               and not QUESTION_SHAPE.search(question)))
    trade_expression = bool(order_directive
                            or (TRADE_ACTION.search(question) and quantity_scenario))
    trade_refusal = trade_expression and (order_directive or not hypothetical)
    earnings = bool(EARNINGS_MARKER.search(question))
    asks_checks = bool(FACT_CHECK_MARKER.search(question))
    follow_up_marker = bool(FOLLOW_UP_MARKER.search(question))

    if explicit_goal is not None:
        goal: Goal = explicit_goal
    elif trade_refusal:
        goal = 'UNSUPPORTED_TRADE'
    elif hypothetical:
        goal = 'HYPOTHETICAL'
    elif PRIORITIZE_MARKER.search(question):
        goal = 'PRIORITIZE'
    elif COMPARE_MARKER.search(question) and len(symbols) + len(ciks) >= 2:
        goal = 'COMPARE'
    elif prior is not None and not (symbols or ciks):
        goal = 'FOLLOW_UP'
    elif symbols or ciks:
        goal = 'INVESTIGATE'
    elif asks_checks:
        goal = 'INVESTIGATE'
    else:
        goal = 'UNKNOWN'

    if goal == 'UNSUPPORTED_TRADE':
        entities, inherited = (), False
    else:
        entities, inherited = _build_entities(symbols=symbols, ciks=ciks, prior=prior,
                                              follow_up=goal == 'FOLLOW_UP')
    primary_symbol = next((e.symbol for e in entities if e.symbol), None)

    clarification: Clarification | None = None
    if len(entities) > 1 and goal != 'COMPARE':
        clarification = Clarification(
            code='MULTIPLE_SYMBOLS',
            question='Which single symbol should this question resolve to?')
    elif goal == 'UNSUPPORTED_TRADE':
        clarification = Clarification(
            code='TRADE_NOT_SUPPORTED',
            question='ConvictionDesk refuses order execution and sizing; this request is recorded as unsupported.')
    elif goal == 'UNKNOWN':
        clarification = Clarification(code='MISSING_GOAL',
                                      question='Which company and which decision should be investigated?')
    elif not entities and goal in {'INVESTIGATE', 'HYPOTHETICAL', 'COMPARE'}:
        clarification = Clarification(code='MISSING_SYMBOL',
                                      question='Which holding or symbol does this question concern?')
    elif trade_expression and hypothetical:
        clarification = Clarification(
            code='TRADE_NOT_SUPPORTED', blocking=False,
            question='Analysis only: no order can be placed from this question.')

    invalidated: list[str] = []
    notes: list[str] = []
    if prior is not None and prior.symbol and primary_symbol and prior.symbol != primary_symbol:
        invalidated += ['thesis', 'price_assertion', 'premises']
        notes.append('SYMBOL_SWITCHED')
    scope = _build_scope(account_scope, prior)
    if prior is not None and scope.source != 'INHERITED' and (
            _scope_signature(prior.account_ids)
            != _scope_signature(scope.account_ids, scope.coverage)):
        invalidated += ['portfolio_context', 'valuation', 'thesis']
        notes.append('ACCOUNT_SCOPE_CHANGED')
    if inherited:
        notes.append('SYMBOL_INHERITED')

    objective = _objective(goal, question, prior)
    premise_rows = _premises(question, explicit, primary_symbol,
                             hypothetical=hypothetical, asserted_at=asserted_at)
    conditions = _conditions(goal, has_price=bool(explicit.prices), asks_checks=asks_checks,
                             earnings=earnings, financials=bool(FINANCIAL_FACT_MARKER.search(question)))

    inherited_fields: list[str] = []
    if inherited:
        inherited_fields.append('symbol')
    if prior is not None and goal == 'FOLLOW_UP':
        inherited_fields.append('goal')
    if prior is not None and scope.source == 'INHERITED':
        inherited_fields.append('account_scope')

    resolved = ResolvedContext(
        task_id=task_id,
        parent_task_id=prior.task_id if prior else None,
        thread_root_id=(prior.thread_root_id or prior.task_id) if prior else None,
        symbol=primary_symbol,
        goal=goal,
        account_scope=scope,
        inherited_fields=tuple(inherited_fields),
        invalidated_fields=tuple(dict.fromkeys(invalidated)),
        memory_notes=tuple(notes))

    intent = AdaptiveIntent(
        question=question.strip(), goal=goal, objective=objective,
        entities=entities, primary_symbol=primary_symbol, account_scope=scope,
        premises=premise_rows, conditions=conditions, clarification=clarification,
        resolved=resolved, literals=explicit, trade_refusal=trade_refusal,
        asks_for_fact_checks=asks_checks, use_news=goal != 'UNSUPPORTED_TRADE',
        use_sec=goal != 'UNSUPPORTED_TRADE',
        sec_query=_sec_query(goal, objective, earnings))
    return merge_semantic(intent, semantic, question=question) if semantic else intent


def _clean_sec_query(query: str, *, forbidden_symbols: tuple[str, ...]) -> str:
    if re.search(r'\d', query):
        raise IntentError('semantic_numeric_query', 'SEC query cannot carry numbers, dates or form codes')
    if SEC_FORM_LITERAL.search(query):
        raise IntentError('semantic_form_query', 'SEC query must not name a form code')
    for symbol in forbidden_symbols:
        if re.search(rf'(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])', query, re.I):
            raise IntentError('semantic_symbol_query', 'SEC query must not repeat the ticker')
    cleaned = ' '.join(query.split())
    if not 3 <= len(cleaned) <= 240:
        raise IntentError('semantic_query_length', 'SEC query length is out of bounds')
    return cleaned


def merge_semantic(intent: AdaptiveIntent, draft: SemanticDraft, *, question: str) -> AdaptiveIntent:
    """Fill parser gaps only; refuse any number, symbol or claim not grounded."""

    allowed_symbols = {e.symbol for e in intent.entities if e.symbol}
    if intent.primary_symbol:
        allowed_symbols.add(intent.primary_symbol)

    if draft.focus_symbol and draft.focus_symbol not in allowed_symbols:
        raise IntentError('semantic_symbol_not_grounded',
                          'draft symbol does not occur in the question or inherited context')
    if draft.issuer_name and draft.issuer_name.casefold() not in question.casefold():
        raise IntentError('semantic_issuer_not_grounded',
                          'draft issuer name does not occur verbatim in the question')

    goal = intent.goal
    conditions = list(intent.conditions)
    clarification = intent.clarification
    if draft.goal and draft.goal != intent.goal:
        if intent.goal != 'UNKNOWN':
            raise IntentError('semantic_goal_conflict',
                              'a deterministic goal marker already resolved this question')
        goal = draft.goal
        conditions = list(_conditions(goal, has_price=bool(intent.literals.prices),
                                      asks_checks=intent.asks_for_fact_checks,
                                      earnings=bool(EARNINGS_MARKER.search(question)),
                                      financials=bool(FINANCIAL_FACT_MARKER.search(question))))
        if clarification is not None and clarification.code in {'MISSING_GOAL', 'UNSUPPORTED_QUESTION'}:
            clarification = None
        if not intent.entities and goal in {'INVESTIGATE', 'HYPOTHETICAL', 'COMPARE'}:
            clarification = Clarification(
                code='MISSING_SYMBOL',
                question='Which holding or symbol does this question concern?')
    if goal == 'UNSUPPORTED_TRADE' and not intent.trade_refusal:
        raise IntentError('semantic_trade_conflict', 'draft cannot turn a research question into a trade')
    if goal == 'FOLLOW_UP' and intent.resolved.parent_task_id is None:
        raise IntentError('semantic_follow_up_without_parent',
                          'a follow-up requires an inherited task')

    seen = {c.condition_id for c in conditions}
    for index, text in enumerate(draft.extra_conditions):
        cleaned = ' '.join(text.split())
        if re.search(r'\d', cleaned):
            raise IntentError('semantic_numeric_condition',
                              'draft condition cannot introduce a number')
        if any(re.search(rf'(?<![A-Za-z0-9]){re.escape(s)}(?![A-Za-z0-9])', cleaned, re.I)
               for s in allowed_symbols):
            raise IntentError('semantic_symbol_condition',
                              'draft condition cannot introduce a ticker')
        if not 6 <= len(cleaned) <= 300:
            raise IntentError('semantic_condition_length', 'draft condition length is out of bounds')
        condition_id = f'draft-condition-{index + 1}'
        if condition_id in seen:
            continue
        conditions.append(NecessaryCondition(
            condition_id=condition_id, question=cleaned, evidence_kind='COMPANY_EVENT',
            required=False, rationale='Model-proposed check; bounded and optional.'))
        seen.add(condition_id)

    sec_query = intent.sec_query
    if draft.sec_query:
        sec_query = _clean_sec_query(draft.sec_query,
                                     forbidden_symbols=tuple(sorted(allowed_symbols)))

    objective = _objective(goal, question, None) if goal != intent.goal else intent.objective
    resolved = intent.resolved.model_copy(update={'goal': goal})
    return intent.model_copy(update={
        'goal': goal,
        'objective': objective,
        'conditions': tuple(conditions),
        'clarification': clarification,
        'sec_query': sec_query,
        'use_news': intent.use_news if draft.use_news is None else intent.use_news and draft.use_news,
        'use_sec': intent.use_sec if draft.use_sec is None else intent.use_sec and draft.use_sec,
        'resolved': resolved,
    })
