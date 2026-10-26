"""Typed evidence-to-action projection, memo contract and deterministic renderer.

The module is an opt-in layer over the existing ResearchTask/ResearchSource
objects. It never fetches evidence, never performs portfolio arithmetic and
never raises the existing Validator's numeric or citation permissions. The
final compiled assessment is checked by the unchanged
``research_task.validate_assessment``.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import Field

from .domain import FrozenModel
from .llm import LLMError
from .research_task import (Identity, ResearchActionAssessment, ResearchAssessment,
                            ResearchEvidenceGap, ResearchStatement, numeric_facts,
                            validate_assessment)

ROLE_RANK = {'USER_ASSERTION': 0, 'OWNER_CONFIRMED_THESIS': 3, 'VERIFIED_FACT': 2,
             'AI_DRAFT': 5, 'HISTORICAL_DECISION': 6, 'UNKNOWN': 7}
EVIDENCE_ID_TOKEN = re.compile(r'(?<![0-9A-Za-z])E[0-9a-fA-F]{12,}(?![0-9A-Za-z])')
URL_TOKEN = re.compile(r'https?://[^\s)\]}>，。；;]+', re.I)
TRADE_LANGUAGE = re.compile(
    r'买入|买进|购入|购买|卖出|卖掉|出售|加仓|减仓|增持|清仓|建仓|'
    r'\b(?:target position|recommended quantity|buy|sell|add|reduce|exit)\b', re.I)
RECEIPT_UNIT_DIMENSION = {'usd': 'usd', 'usd_per_share': 'usd', 'shares': 'shares',
                          'ratio': 'ratio', 'percent': 'percent', 'multiple': 'ratio',
                          'seconds': None, 'value': None}
MAX_REFS = 18
MAX_EXCERPT = 600
MAX_RECEIPTS = 64
PROMPT_VERSION = 'evidence-action-v4'
MEMO_VERSION = 'evidence-action-memo-v1'
MEMO_CHAR_BUDGET = 3200
MIXED_PROVENANCE_MARKER = re.compile(
    r'用户所述|用户提供|用户表示|用户称|用户问题|user (?:said|stated|provided|asked)|'
    r'completed close|最近.*收盘|另一来源|另一份|其他来源|市场数据', re.I)
SAFE_TRADE_CONTEXT = re.compile(
    r'(?:加仓|减仓|买入|卖出|增持|清仓|建仓)(?:的|所)?'
    r'(?:风险|假设|前提|条件|依据|逻辑|研究|判断|影响|可能性|依赖)|'
    # Only a direct negation of the trade verb is non-advisory ("不足以支持加仓");
    # a bare 不 with a gap would admit advice such as "不难看出应该加仓".
    r'(?:不应|不宜|不能|无法|并非|不是|不构成|不足以|不建议)'
    r'(?:支持|据此|立即|马上|贸然|现在|在此|直接)?\s*(?:加仓|减仓|买入|卖出)', re.I)


class MemoContractError(LLMError):
    def __init__(self, code: str, message: str, *, field: str | None = None,
                 diagnostic: dict | None = None):
        super().__init__(code, message)
        self.diagnostic = {'reason': code, 'field': field, **(diagnostic or {})}


class EvidenceRef(FrozenModel):
    ref: str
    evidence_id: str
    identity: Identity
    freshness: Literal['CURRENT', 'HISTORICAL', 'STALE', 'NOT_VERIFIED']
    evidence_type: str
    title: str | None = None
    symbol: str | None = None
    published_at: datetime | None = None
    as_of: datetime | None = None
    retrieved_at: datetime
    age_seconds: int | None = None
    age_basis: Literal['published_at', 'as_of', 'retrieved_at'] | None = None
    retrieved_after_seconds: int | None = None
    excerpt: str | None = None
    excerpt_truncated: bool = False
    source_hash: str
    source_uri: str | None = None
    structured_data: dict | None = None
    receipt_ids: list[str] = []


class NumericReceipt(FrozenModel):
    receipt_id: str
    evidence_ref: str
    evidence_id: str
    field: str
    value: str
    unit: Literal['usd', 'usd_per_share', 'shares', 'ratio', 'percent',
                  'multiple', 'seconds', 'value']
    as_of: datetime | None = None
    period: dict | None = None
    basis: str | None = None


class EvidencePacket(FrozenModel):
    version: Literal['evidence-action-v4'] = PROMPT_VERSION
    question: str
    objective: str
    symbol: str
    cutoff: datetime
    mode: str
    refs: list[EvidenceRef]
    receipts: list[NumericReceipt]
    position_status: Literal['HELD', 'NOT_HELD', 'UNKNOWN']
    portfolio_scope: dict
    premise: dict
    limitations: list[str]
    investigation: dict
    omitted: list[dict]
    packet_hash: str

    def ref(self, ref: str) -> EvidenceRef | None:
        wanted = str(ref or '').strip().upper()
        return next((row for row in self.refs if row.ref == wanted), None)

    def receipt(self, receipt_id: str) -> NumericReceipt | None:
        wanted = str(receipt_id or '').strip().upper()
        return next((row for row in self.receipts if row.receipt_id == wanted), None)

    def ref_for_evidence(self, evidence_id: str) -> str | None:
        return next((row.ref for row in self.refs if row.evidence_id == evidence_id), None)

    def citation_map(self) -> dict[str, str]:
        return {row.ref: row.evidence_id for row in self.refs}


class MemoClaim(FrozenModel):
    text: str = Field(min_length=1, max_length=500)
    kind: Literal['FACT', 'INFERENCE', 'GAP']
    refs: list[str] = Field(default=[], max_length=6)
    receipts: list[str] = Field(default=[], max_length=8)


class MemoConclusion(FrozenModel):
    decision: Literal['ADD', 'HOLD', 'REDUCE', 'EXIT', 'NONE']
    status: Literal['SUPPORTED', 'PROVISIONAL', 'UNDETERMINED']
    strength: Literal['STRONG', 'MIXED', 'LIMITED', 'INSUFFICIENT']
    text: str = Field(min_length=1, max_length=500)
    refs: list[str] = Field(default=[], max_length=6)
    receipts: list[str] = Field(default=[], max_length=8)


class MemoGap(FrozenModel):
    tool: Literal['NEWS', 'SEC']
    query: str = Field(min_length=3, max_length=240)
    material_question: str = Field(min_length=8, max_length=400)
    why_material: str = Field(min_length=8, max_length=400)
    expected_information: str = Field(min_length=8, max_length=400)
    refs: list[str] = Field(min_length=1, max_length=6)


class GroundedMemo(FrozenModel):
    version: Literal['evidence-action-memo-v1'] = MEMO_VERSION
    conclusion: MemoConclusion
    key_reasons: list[MemoClaim] = Field(min_length=1, max_length=3)
    strongest_counterevidence: MemoClaim
    change_conditions: list[MemoClaim] = Field(min_length=1, max_length=3)
    next_check: MemoClaim
    thesis_delta: MemoClaim | None = None
    investigation_effect: MemoClaim | None = None
    judgment_change: Literal['CHANGED', 'UNCHANGED', 'UNRESOLVED'] | None = None
    gap: MemoGap | None = None

    def total_characters(self) -> int:
        rows = [self.conclusion.text, self.strongest_counterevidence.text,
                self.next_check.text, *(row.text for row in self.key_reasons),
                *(row.text for row in self.change_conditions)]
        if self.thesis_delta:
            rows.append(self.thesis_delta.text)
        if self.investigation_effect:
            rows.append(self.investigation_effect.text)
        return sum(len(row) for row in rows)


PROMPT_TEMPLATE = """你是证据约束的股票研究模型。只输出一个 JSON 对象，不要 Markdown、解释或推理过程。

硬性规则：
1. 只能使用 evidence 和 numeric_receipts。禁止写入原始 evidence_id、URL、哈希或新标的。
2. claim.kind: FACT 表示可直接核对的来源事实，只能引用同一 identity 的 VERIFIED_FACT 来源；INFERENCE 表示推断、条件或因果假设；GAP 表示尚未解决的事实或核查。
3. 每个数字都必须来自被引用来源的 excerpt，或在 receipts 中引用对应 numeric_receipt，并原样照抄 value（含小数位）。用户价格或数量必须引用 user_price/user_quantity receipt。禁止计算、换算、四舍五入、百分比推导；不要写秒数或时间差，时效只用日期和 CURRENT/STALE 表述。
4. conclusion.decision 只有在 objective 要求且存在 CURRENT 公司证据时才可给 ADD/REDUCE。证据不足时必须 NONE + UNDETERMINED，不得默认 HOLD。HOLD/REDUCE/EXIT 还要求 position_status=HELD。
5. 先给结论，再给最多 3 条关键理由、1 条最强反证、1-3 条可证伪的改变条件、1 条最高信息量的下一步核查。
6. 交易动词（买入/卖出/加仓/减仓，含否定句）只能出现在 conclusion。
7. 引用 STALE/HISTORICAL 来源时必须明确写成“历史/此前”，不得当作当前事实。不得把 USER_ASSERTION 或 AI_DRAFT 升级为已核实事实。
8. 每个 claim 都至少引用一个 S 编号。中文输出，单条不超过 500 字。
9. objective 为 EXPLAIN_CHANGE 或 EARNINGS_CHECK 时用户没有请求交易方向：decision 用 NONE，结论写研究回答本身。
10. investigation 存在时必须填 investigation_effect 与 judgment_change；最多一个 gap。
11. 不复述持仓股数、成本、账户金额；确需时写“当前持有 N 股”并引用组合计算与 receipt。
12. 用户问题中的数字写明“用户所述”并引用其 USER_ASSERTION 的 S 编号。
"""


def _letters(prefix: str, key: str, length: int) -> str:
    digest = hashlib.sha256(key.encode('utf-8')).digest()
    number = int.from_bytes(digest[:8], 'big')
    letters = ''.join(chr(65 + ((number >> (5 * index)) & 31) % 26) for index in range(length))
    return prefix + letters


def _unique_ref(prefix: str, key: str, used: set[str], start: int = 6) -> str:
    for length in range(start, start + 4):
        candidate = _letters(prefix, key, length)
        if candidate not in used:
            used.add(candidate)
            return candidate
    raise MemoContractError('ref_collision', 'evidence reference space exhausted', field='refs')


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip().replace(',', ''))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _receipt_unit(unit, field):
    text = str(unit or '').strip().lower()
    if text in {'usd', 'usd/shares', 'usd per share', 'usd_per_share'}:
        return 'usd_per_share' if 'share' in text else 'usd'
    if text in {'shares', 'share'}:
        return 'shares'
    if text in {'ratio', 'multiple', 'x'}:
        return 'multiple' if text == 'multiple' else 'ratio'
    if text in {'percent', '%', 'percentage'}:
        return 'percent'
    if 'weight' in field or field.endswith('percent') or field.endswith('_pnl_percent'):
        return 'ratio'
    if field in {'price', 'average_cost'} or field.endswith('.price'):
        return 'usd_per_share'
    if field.endswith('quantity'):
        return 'shares'
    if field in {'cash', 'nav', 'total_cost', 'market_value', 'unrealized_pnl', 'cost_basis'}:
        return 'usd'
    return 'value'


def _age(ref: EvidenceRef, cutoff: datetime):
    candidates = []
    if ref.evidence_type in {'market_data', 'calculation'} and ref.as_of:
        candidates.append(('as_of', ref.as_of))
    if ref.published_at:
        candidates.append(('published_at', ref.published_at))
    if ref.as_of:
        candidates.append(('as_of', ref.as_of))
    candidates.append(('retrieved_at', ref.retrieved_at))
    basis, stamp = candidates[0]
    if stamp > cutoff:
        return None, None, None
    age = int((cutoff - stamp).total_seconds())
    retrieved_after = int((ref.retrieved_at - stamp).total_seconds())
    return age, basis, max(0, retrieved_after)


def _source_priority(source):
    evidence = source.evidence
    if source.identity == 'USER_ASSERTION':
        value = 0
    elif source.identity == 'VERIFIED_FACT' and source.freshness == 'CURRENT' and evidence.evidence_type in {'news', 'sec_filing'}:
        value = 1
    elif source.identity == 'VERIFIED_FACT' and source.freshness == 'CURRENT' and evidence.evidence_type == 'calculation':
        value = 2
    elif source.identity == 'VERIFIED_FACT' and source.freshness == 'CURRENT':
        value = 3
    else:
        value = ROLE_RANK[source.identity] + (2 if source.freshness == 'STALE' else 0)
    stamp = evidence.published_at or evidence.as_of or evidence.retrieved_at
    return value, -stamp.timestamp(), evidence.evidence_id


def _eligible(sources, task):
    rows = []
    for source in sources:
        evidence = source.evidence
        if evidence.published_at and evidence.published_at > task.cutoff:
            continue
        if evidence.as_of and evidence.as_of > task.cutoff:
            continue
        if evidence.evidence_type not in {'news', 'sec_filing'} and evidence.retrieved_at > task.cutoff:
            continue
        if (evidence.symbol and evidence.symbol != task.focus_symbol
                and evidence.evidence_type in {'news', 'sec_filing', 'thesis', 'semantic_judgment'}):
            continue
        rows.append(source)
    return rows


def _position_status(task):
    context = task.portfolio_context
    if not context or not context.get('snapshot_id'):
        return 'UNKNOWN'
    position = context.get('position')
    if position and position.get('symbol') == task.focus_symbol:
        quantity = _decimal(position.get('quantity'))
        return 'HELD' if quantity is not None and quantity > 0 else 'NOT_HELD'
    return 'NOT_HELD'


def _receipt_rows(evidence):
    data = evidence.structured_data or {}
    rows = []

    def add(field, value, unit=None, *, as_of=None, period=None, basis=None):
        number = _decimal(value)
        if number is None:
            return
        resolved = _receipt_unit(unit, field)
        rows.append({'field': field, 'value': str(number), 'unit': resolved,
                     'as_of': as_of or evidence.as_of, 'period': period, 'basis': basis})

    calculation = data.get('calculation')
    if calculation == 'task_valuation_v1':
        for name, metric in (data.get('metrics') or {}).items():
            if not isinstance(metric, dict):
                continue
            add('valuation.' + str(name), metric.get('value'), metric.get('unit'),
                as_of=evidence.as_of, period=metric.get('period'), basis=metric.get('data_status'))
    elif calculation == 'sec_quarterly_v1':
        quarter = data.get('quarter') or {}
        for row in quarter.get('metrics') or []:
            if not isinstance(row, dict):
                continue
            add('quarter.' + str(row.get('metric')), row.get('value'), row.get('unit'),
                as_of=evidence.as_of, period={'start': row.get('start'), 'end': row.get('end')},
                basis=row.get('basis'))
    elif evidence.evidence_type == 'market_data':
        add('price', data.get('price'), 'usd_per_share', as_of=evidence.as_of,
            period={'instant': evidence.as_of.isoformat()} if evidence.as_of else None,
            basis=data.get('basis') or data.get('session'))
    elif evidence.evidence_type == 'semantic_judgment':
        asserted_at = evidence.as_of
        if data.get('price_asserted_at'):
            try:
                asserted_at = datetime.fromisoformat(data['price_asserted_at'])
                if asserted_at.tzinfo is None or asserted_at > evidence.as_of:
                    asserted_at = None
            except (ValueError,TypeError):
                asserted_at = None
        if asserted_at is not None:
            add('user_price', data.get('price'), 'usd_per_share', as_of=asserted_at,
                period={'instant':asserted_at.isoformat()},basis='USER_ASSERTION_PREMISE')
        add('user_quantity', data.get('quantity'), 'shares', as_of=evidence.as_of, basis='USER_ASSERTION_PREMISE')
    elif evidence.evidence_type == 'calculation':
        for field in ('cash', 'nav', 'total_cost'):
            add(field, data.get(field), 'usd')
        position = data.get('position') or {}
        if isinstance(position, dict):
            for field, unit in (('quantity', 'shares'), ('average_cost', 'usd_per_share'),
                                ('cost_basis', 'usd'), ('price', 'usd_per_share'),
                                ('market_value', 'usd'), ('unrealized_pnl', 'usd')):
                add('position.' + field, position.get(field), unit)
            # Raw 28-digit ratios are unreadable; the evidence already carries Python
            # display percents, which the unchanged Validator also tokenizes exactly.
            for field in ('nav_weight_percent_display', 'unrealized_pnl_percent_display'):
                display = position.get(field)
                if isinstance(display, str) and display.endswith('%'):
                    add('position.' + field, display[:-1], 'percent')
            for index, account in enumerate(position.get('accounts') or []):
                if not isinstance(account, dict):
                    continue
                add(f'position.accounts[{index}].quantity', account.get('quantity'), 'shares')
                add(f'position.accounts[{index}].average_cost', account.get('average_cost'), 'usd_per_share')
    return rows[:MAX_RECEIPTS]


def build_evidence_packet(task, *, sources=None) -> EvidencePacket:
    selected = list(sources if sources is not None else _eligible(task.sources, task))
    selected.sort(key=_source_priority)
    used: set[str] = set()
    receipt_used: set[str] = set()
    refs: list[EvidenceRef] = []
    receipts: list[NumericReceipt] = []
    for source in selected[:MAX_REFS]:
        evidence = source.evidence
        ref = _unique_ref('S', evidence.evidence_id, used)
        excerpt = evidence.excerpt
        truncated = bool(excerpt and len(excerpt) > MAX_EXCERPT)
        row = EvidenceRef(ref=ref, evidence_id=evidence.evidence_id, identity=source.identity,
            freshness=source.freshness, evidence_type=evidence.evidence_type,
            title=evidence.title, symbol=evidence.symbol, published_at=evidence.published_at,
            as_of=evidence.as_of, retrieved_at=evidence.retrieved_at,
            excerpt=(excerpt or '')[:MAX_EXCERPT] or None, excerpt_truncated=truncated,
            source_hash=evidence.content_hash, source_uri=evidence.source_uri,
            structured_data=evidence.structured_data)
        age, basis, retrieved_after = _age(row, task.cutoff)
        row = row.model_copy(update={'age_seconds': age, 'age_basis': basis,
                                     'retrieved_after_seconds': retrieved_after})
        # Ages stay on the ref for audit only. Seconds are not research facts and the
        # unchanged Validator cannot trace them to saved evidence (NORTHSTAR-001 live run).
        derived = []
        for item in _receipt_rows(evidence):
            receipt_id = _unique_ref('N', f'{evidence.evidence_id}:{item["field"]}', receipt_used)
            receipts.append(NumericReceipt(receipt_id=receipt_id, evidence_ref=ref,
                evidence_id=evidence.evidence_id, field=item['field'], value=item['value'],
                unit=item['unit'], as_of=item['as_of'], period=item['period'], basis=item['basis']))
            derived.append(receipt_id)
        refs.append(row.model_copy(update={'receipt_ids': derived}))
    portfolio_scope = {}
    if task.portfolio_context:
        context = task.portfolio_context
        portfolio_scope = {'account_id': context.get('account_id'),
            'account_names': context.get('account_names'), 'basis': context.get('basis'),
            'valuation_status': context.get('valuation_status'),
            'missing_price_symbols': context.get('missing_price_symbols'),
            'position_status': _position_status(task),
            'numbers_in': 'numeric_receipts'}
    omitted = [{'evidence_id': source.evidence.evidence_id, 'identity': source.identity,
                'reason': 'source-count or projection limit'}
               for source in selected[MAX_REFS:]]
    payload = {'version': PROMPT_VERSION, 'question': task.question, 'objective': task.objective,
        'symbol': task.focus_symbol, 'cutoff': task.cutoff, 'mode': getattr(task, 'mode', 'DEEP'),
        'refs': [row.model_dump(mode='json') for row in refs],
        'receipts': [row.model_dump(mode='json') for row in receipts],
        'position_status': _position_status(task), 'portfolio_scope': portfolio_scope,
        'premise': task.premise, 'limitations': list(task.limitations),
        'investigation': task.investigation, 'omitted': omitted}
    return EvidencePacket(**payload, packet_hash=hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest())


def prompt_payload(packet: EvidencePacket) -> dict:
    payload = {'version': packet.version, 'question': packet.question, 'objective': packet.objective,
        'symbol': packet.symbol, 'cutoff': packet.cutoff.isoformat(), 'mode': packet.mode,
        'position_status': packet.position_status, 'portfolio_scope': packet.portfolio_scope,
        'premise': packet.premise, 'limitations': packet.limitations,
        'investigation': packet.investigation,
        'evidence': [{'ref': row.ref, 'identity': row.identity, 'freshness': row.freshness,
            'type': row.evidence_type, 'title': row.title, 'symbol': row.symbol,
            'published_at': row.published_at.isoformat() if row.published_at else None,
            'as_of': row.as_of.isoformat() if row.as_of else None,
            'excerpt': row.excerpt, 'excerpt_truncated': row.excerpt_truncated,
            'receipt_ids': row.receipt_ids} for row in packet.refs],
        'numeric_receipts': [{'id': row.receipt_id, 'ref': row.evidence_ref, 'field': row.field,
            'value': row.value, 'unit': row.unit,
            'as_of': row.as_of.isoformat() if row.as_of else None,
            'period': row.period, 'basis': row.basis} for row in packet.receipts]}
    canonical = {row.evidence_id:row.ref for row in packet.refs}

    def references(value):
        if isinstance(value,dict):
            return {('new_evidence_refs' if key=='new_evidence_ids' else 'refs' if key=='evidence_ids' else key):references(item)
                for key,item in value.items()}
        if isinstance(value,list):
            return [item for row in value if (item:=references(row)) is not None]
        if isinstance(value,str):
            if value in canonical:
                return canonical[value]
            if EVIDENCE_ID_TOKEN.fullmatch(value):
                return None  # omitted sources never acquire a prompt reference
        return value

    return references(payload)


def system_prompt() -> str:
    return PROMPT_TEMPLATE


def contract_request(packet: EvidencePacket) -> dict:
    return {'purpose': 'research.assess', 'response_model': GroundedMemo,
            'facts': prompt_payload(packet), 'system_instruction': system_prompt(),
            'evidence_ids': [row.ref for row in packet.refs],
            'numeric_fact_tokens': sorted({token for row in packet.receipts
                for token in numeric_facts(row.value, RECEIPT_UNIT_DIMENSION[row.unit])})}


def sanitize_claim_text(text: str, packet: EvidencePacket):
    replacements = []

    def evidence(match):
        token = match.group()
        replacements.append(token)
        ref = packet.ref_for_evidence(token)
        return f'[{ref}]' if ref else '[evidence]'

    sanitized = URL_TOKEN.sub('[link]', text)
    sanitized = EVIDENCE_ID_TOKEN.sub(evidence, sanitized)
    return sanitized, replacements


def _resolve_refs(refs, packet: EvidencePacket, field: str):
    rows = []
    for value in refs:
        row = packet.ref(value)
        if row is None:
            raise MemoContractError('unknown_evidence_ref',
                'claim cites an evidence reference that is not in this packet', field=field,
                diagnostic={'ref': str(value)[:24], 'available': [r.ref for r in packet.refs]})
        rows.append(row)
    if not rows:
        raise MemoContractError('citation_required', 'every claim must cite at least one supplied source',
                                field=field)
    return rows


def _claim_identity(claim: MemoClaim, packet: EvidencePacket, *, field: str, future=False):
    rows = _resolve_refs(claim.refs, packet, field)
    if future:
        return 'AI_DRAFT', rows
    if claim.kind == 'INFERENCE':
        return 'AI_DRAFT', rows
    if claim.kind == 'GAP':
        return 'UNKNOWN', rows
    identities = {row.identity for row in rows}
    if len(identities) != 1:
        if ({'USER_ASSERTION', 'VERIFIED_FACT'} <= identities
                and MIXED_PROVENANCE_MARKER.search(claim.text)):
            return 'AI_DRAFT', rows
        raise MemoContractError('unsupported_source_identity',
            'FACT claims cannot mix source identities; use INFERENCE for a synthesis',
            field=field, diagnostic={'identities': sorted(identities)})
    identity = next(iter(identities))
    if identity in {'AI_DRAFT', 'UNKNOWN'}:
        raise MemoContractError('unsupported_source_identity',
            'a FACT claim requires a verified or explicitly attributed source', field=field,
            diagnostic={'identity': identity})
    if SAFE_TRADE_CONTEXT.search(claim.text):
        return 'AI_DRAFT', rows
    return identity, rows


def _check_text(text, rows, receipts, packet, *, field, block_action=False, fact=False):
    sanitized, replaced = sanitize_claim_text(text, packet)
    if block_action and TRADE_LANGUAGE.search(sanitized):
        raise MemoContractError('action_language_outside_conclusion',
            'trade action language is only allowed in conclusion.decision', field=field)
    if fact and packet.symbol and packet.symbol in sanitized.upper() and rows and all(
            row.symbol and row.symbol != packet.symbol for row in rows):
        focus = re.escape(packet.symbol)
        # A disclaimer exempts only its own mention. A later positive claim about
        # the focus issuer must not inherit the exemption from unrelated prose.
        disclaimer = re.compile(
            rf'(?:与|和|同|跟)\s*{focus}\s*(?:无关|无涉|不符|不同|不是|非)'
            rf'|(?:不是|并非|而非|not)\s*{focus}', re.I)
        remaining = disclaimer.sub('', sanitized)
        remaining = re.sub(
            rf'(?:不能|不可|无法|不足以)\s*(?:作为|替代|代表|证明|用于)\s*{focus}',
            '', remaining, flags=re.I)
        if not disclaimer.search(sanitized) or packet.symbol in remaining.upper():
            raise MemoContractError('symbol_mismatch',
                'another symbol source cannot be stated as a fact about the focus symbol', field=field,
                diagnostic={'symbol': packet.symbol, 'source_symbols': sorted({row.symbol for row in rows})})
    cited_ids = {row.evidence_id for row in rows}
    cited_receipts = []
    for value in receipts:
        receipt = packet.receipt(value)
        if receipt is None:
            raise MemoContractError('unknown_numeric_receipt',
                'claim cites a numeric receipt that is not in this packet', field=field,
                diagnostic={'receipt': str(value)[:24]})
        if receipt.evidence_id not in cited_ids:
            raise MemoContractError('receipt_not_cited',
                'a numeric receipt must belong to a source cited by the same claim', field=field,
                diagnostic={'receipt': receipt.receipt_id, 'ref': receipt.evidence_ref})
        cited_receipts.append(receipt)
    evidence_numbers = set().union(*(
        numeric_facts({'evidence_type': row.evidence_type, 'excerpt': row.excerpt,
                       'title': row.title, 'as_of': row.as_of, 'published_at': row.published_at,
                       'retrieved_at': row.retrieved_at, 'structured_data': row.structured_data})
        for row in rows)) if rows else set()
    excerpt_numbers = set().union(*(numeric_facts(row.excerpt or '') for row in rows)) if rows else set()
    receipt_numbers = set()
    for receipt in cited_receipts:
        dimension = RECEIPT_UNIT_DIMENSION[receipt.unit]
        receipt_numbers.update(numeric_facts(receipt.value, dimension))
        receipt_numbers.add('value:' + str(Decimal(receipt.value).normalize()))
    missing_receipt = []
    for token in numeric_facts(sanitized):
        if token in receipt_numbers or token in excerpt_numbers:
            continue
        if token.startswith(('date:', 'year:')) and token in evidence_numbers:
            continue
        if token in evidence_numbers:
            missing_receipt.append(token)
            continue
        raise MemoContractError('unsupported_numeric_fact',
            'claim contains a number not present in its cited evidence or receipts', field=field,
            diagnostic={'token': token, 'receipts': [r.receipt_id for r in cited_receipts]})
    if missing_receipt:
        raise MemoContractError('numeric_receipt_required',
            'structured-data numbers must be quoted through a numeric receipt', field=field,
            diagnostic={'tokens': sorted(missing_receipt)[:6]})
    return sanitized, replaced


def validate_grounded_memo(memo: GroundedMemo, packet: EvidencePacket) -> GroundedMemo:
    if memo.total_characters() > MEMO_CHAR_BUDGET:
        raise MemoContractError('memo_too_long', 'memo exceeds the compactness budget', field='memo',
                                diagnostic={'characters': memo.total_characters(), 'max': MEMO_CHAR_BUDGET})
    conclusion_rows = _resolve_refs(memo.conclusion.refs, packet, 'conclusion')
    conclusion_text, _ = _check_text(memo.conclusion.text, conclusion_rows,
                                     memo.conclusion.receipts, packet, field='conclusion')
    if memo.conclusion.decision == 'NONE':
        # Status qualifies a direction; NONE has none, so normalize instead of failing
        # a research answer on a meaningless field (NORTHSTAR-001 live DeepSeek case).
        if memo.conclusion.status != 'UNDETERMINED':
            memo = memo.model_copy(update={'conclusion': memo.conclusion.model_copy(update={'status': 'UNDETERMINED'})})
    else:
        if memo.conclusion.status == 'UNDETERMINED':
            raise MemoContractError('action_status_mismatch',
                'a direction cannot stay UNDETERMINED', field='conclusion')
        if memo.conclusion.strength == 'INSUFFICIENT':
            raise MemoContractError('action_status_mismatch',
                'INSUFFICIENT evidence cannot authorize a direction', field='conclusion')
        if memo.conclusion.decision in {'HOLD', 'REDUCE', 'EXIT'} and packet.position_status != 'HELD':
            raise MemoContractError('position_required',
                'HOLD/REDUCE/EXIT require a held position in the selected scope', field='conclusion')
        if memo.conclusion.decision == 'ADD' and packet.objective != 'CONSIDER_ADD':
            raise MemoContractError('objective_mismatch', 'ADD is outside this objective', field='conclusion')
        if memo.conclusion.decision == 'REDUCE' and packet.objective != 'CONSIDER_REDUCE':
            raise MemoContractError('objective_mismatch', 'REDUCE is outside this objective', field='conclusion')
        if not any(row.identity == 'VERIFIED_FACT' and row.freshness == 'CURRENT'
                   and row.evidence_type in {'news', 'sec_filing'} for row in conclusion_rows):
            raise MemoContractError('insufficient_action_evidence',
                'a direction requires cited CURRENT company evidence', field='conclusion')
    normalized_reasons = []
    for index, claim in enumerate(memo.key_reasons):
        identity, rows = _claim_identity(claim, packet, field=f'key_reasons[{index}]')
        text, _ = _check_text(claim.text, rows, claim.receipts, packet, field=f'key_reasons[{index}]',
                              block_action=identity not in {'AI_DRAFT', 'UNKNOWN'},
                              fact=identity not in {'AI_DRAFT', 'UNKNOWN'})
        normalized_reasons.append(claim.model_copy(update={'text': text}))
    counter = memo.strongest_counterevidence
    identity, rows = _claim_identity(counter, packet, field='strongest_counterevidence')
    counter_text, _ = _check_text(counter.text, rows, counter.receipts, packet,
                                  field='strongest_counterevidence',
                                  block_action=identity not in {'AI_DRAFT', 'UNKNOWN'},
                                  fact=identity not in {'AI_DRAFT', 'UNKNOWN'})
    normalized_conditions = []
    for index, claim in enumerate(memo.change_conditions):
        _, rows = _claim_identity(claim, packet, field=f'change_conditions[{index}]', future=True)
        text, _ = _check_text(claim.text, rows, claim.receipts, packet, field=f'change_conditions[{index}]')
        normalized_conditions.append(claim.model_copy(update={'text': text}))
    _, rows = _claim_identity(memo.next_check, packet, field='next_check')
    next_text, _ = _check_text(memo.next_check.text, rows, memo.next_check.receipts, packet,
                               field='next_check')
    thesis_delta = memo.thesis_delta
    if thesis_delta:
        _, rows = _claim_identity(thesis_delta, packet, field='thesis_delta')
        delta_text, _ = _check_text(thesis_delta.text, rows, thesis_delta.receipts, packet,
                                    field='thesis_delta')
        thesis_delta = thesis_delta.model_copy(update={'text': delta_text})
    effect = memo.investigation_effect
    if effect:
        _, rows = _claim_identity(effect, packet, field='investigation_effect')
        effect_text, _ = _check_text(effect.text, rows, effect.receipts, packet,
                                     field='investigation_effect')
        effect = effect.model_copy(update={'text': effect_text})
    return memo.model_copy(update={'conclusion': memo.conclusion.model_copy(update={'text': conclusion_text}),
        'key_reasons': normalized_reasons, 'strongest_counterevidence': counter.model_copy(
            update={'text': counter_text}), 'change_conditions': normalized_conditions,
        'next_check': memo.next_check.model_copy(update={'text': next_text}),
        'thesis_delta': thesis_delta, 'investigation_effect': effect})


def _disposition(memo: GroundedMemo, packet: EvidencePacket):
    decision = memo.conclusion.decision
    if decision == 'ADD':
        return 'CONDITIONAL_ADD' if packet.objective == 'CONSIDER_ADD' else 'DEFER'
    if decision == 'REDUCE':
        return 'REVIEW_REDUCE' if packet.objective == 'CONSIDER_REDUCE' else 'DEFER'
    if decision in {'HOLD', 'EXIT'}:
        return 'RESEARCH_ONLY'
    current_company = any(row.identity == 'VERIFIED_FACT' and row.freshness == 'CURRENT'
                          and row.evidence_type in {'news', 'sec_filing'} for row in packet.refs)
    if packet.objective == 'EARNINGS_CHECK' and current_company:
        return 'RESEARCH_ONLY'
    return 'DEFER' if current_company or memo.gap or packet.refs else 'UNKNOWN'


def _statement(claim: MemoClaim, packet: EvidencePacket, *, identity: str | None = None,
               future=False):
    derived, rows = _claim_identity(claim, packet, field='compile', future=future)
    return ResearchStatement(text=claim.text, identity=(identity or derived),
                             evidence_ids=[row.evidence_id for row in rows])


def compile_memo(memo: GroundedMemo, packet: EvidencePacket, sources) -> ResearchAssessment:
    normalized = validate_grounded_memo(memo, packet)
    conclusion_ids = [row.evidence_id for row in _resolve_refs(
        normalized.conclusion.refs, packet, 'conclusion')]
    disposition = _disposition(normalized, packet)
    conditions = [_statement(claim, packet, identity='AI_DRAFT', future=True)
                  for claim in normalized.change_conditions]
    next_check = _statement(normalized.next_check, packet)
    gap = None
    if normalized.gap:
        gap = ResearchEvidenceGap(tool=normalized.gap.tool, query=normalized.gap.query,
            material_question=normalized.gap.material_question,
            why_material=normalized.gap.why_material,
            expected_information=normalized.gap.expected_information,
            evidence_ids=[row.evidence_id for row in _resolve_refs(
                normalized.gap.refs, packet, 'gap')])
    thesis_delta = normalized.thesis_delta
    if thesis_delta is None:
        thesis_delta = MemoClaim(text='本轮结论没有改变对既有论点的确认状态。', kind='INFERENCE',
                                 refs=normalized.conclusion.refs, receipts=[])
    # Status qualifies a direction; with NONE there is none, so it is UNDETERMINED by definition.
    no_direction = normalized.conclusion.decision == 'NONE'
    action = ResearchActionAssessment(
        direction=None if no_direction else normalized.conclusion.decision,
        status='UNDETERMINED' if no_direction else normalized.conclusion.status, evidence_strength=normalized.conclusion.strength,
        reason=ResearchStatement(text=normalized.conclusion.text, identity='AI_DRAFT',
                                 evidence_ids=conclusion_ids),
        thesis_delta=_statement(thesis_delta, packet))
    assessment = ResearchAssessment(
        disposition=disposition,
        core_judgment=ResearchStatement(text=normalized.conclusion.text, identity='AI_DRAFT',
                                        evidence_ids=conclusion_ids),
        key_reasons=[_statement(claim, packet) for claim in normalized.key_reasons],
        support=[],
        counterevidence=[_statement(normalized.strongest_counterevidence, packet)],
        action_conditions=conditions,
        next_checks=[next_check],
        supplemental_query=None,
        evidence_gap=gap,
        investigation_effect=(_statement(normalized.investigation_effect, packet)
                              if normalized.investigation_effect else None),
        judgment_change=normalized.judgment_change,
        action=action)
    assessment = ResearchAssessment.model_validate(assessment.model_dump())
    try:
        validate_assessment(assessment, sources, objective=packet.objective)
    except LLMError as exc:
        raise MemoContractError(exc.code, 'compiled memo failed the unchanged evidence validator',
            field='compile', diagnostic=getattr(exc, 'diagnostic', {'reason': exc.code})) from None
    return assessment


def render_memo(memo: GroundedMemo, packet: EvidencePacket) -> str:
    def cite(row):
        refs = ','.join(str(ref).upper() for ref in row.refs)
        receipts = ','.join(str(receipt).upper() for receipt in row.receipts)
        suffix = ' [' + refs + (']' if not receipts else '; ' + receipts + ']') if refs else ''
        return suffix

    lines = ['【结论】' + memo.conclusion.text + cite(memo.conclusion), '【关键理由】']
    lines.extend(f'{index}. {row.text}{cite(row)}' for index, row in enumerate(memo.key_reasons, 1))
    lines.append('【最强反证】' + memo.strongest_counterevidence.text + cite(memo.strongest_counterevidence))
    lines.append('【改变条件】')
    lines.extend(f'{index}. {row.text}{cite(row)}' for index, row in enumerate(memo.change_conditions, 1))
    lines.append('【下一步核查】' + memo.next_check.text + cite(memo.next_check))
    return '\n'.join(lines)


def explain_failure(error: MemoContractError, packet: EvidencePacket, *, attempt: int = 1) -> str:
    available_refs = ', '.join(row.ref for row in packet.refs)
    available_receipts = ', '.join(row.receipt_id for row in packet.receipts[:12])
    guidance = {
        'unknown_evidence_ref': f'只能使用已提供的 S 编号：{available_refs}。',
        'citation_required': '每条理由、反证和条件都必须至少引用一个 S 编号。',
        'unsupported_source_identity': 'FACT 只能引用同一 identity 的已核实来源；跨来源综合请用 INFERENCE。',
        'unsupported_numeric_fact': '数字必须出现在被引用来源的 excerpt 中，或引用 numeric_receipts。',
        'numeric_receipt_required': f'结构化数字必须引用 numeric receipt：{available_receipts}。',
        'unknown_numeric_receipt': '只能引用 numeric_receipts 中存在的 N 编号。',
        'receipt_not_cited': '引用的 receipt 必须属于同一 claim 引用的 S 来源。',
        'action_language_outside_conclusion': '理由、反证和条件中不要出现买入/卖出/加仓/减仓；行动只放在 conclusion.decision。',
        'action_status_mismatch': 'decision=NONE 必须配 UNDETERMINED；给出方向时不能是 UNDETERMINED 或 INSUFFICIENT。',
        'insufficient_action_evidence': '给出 ADD/REDUCE 前，结论必须引用 CURRENT 的新闻或 SEC 文件。',
        'objective_mismatch': 'ADD 只用于 CONSIDER_ADD；REDUCE 只用于 CONSIDER_REDUCE。',
        'symbol_mismatch': '其他标的的价格或文件不能写成当前研究标的的事实；只能作为错配说明或 GAP。',
        'position_required': '只有 position_status=HELD 才能给 HOLD/REDUCE/EXIT。',
        'memo_too_long': f'整份输出不得超过 {MEMO_CHAR_BUDGET} 字。',
        'stale_current_claim': '过期来源只能作为历史背景，并明确写出“历史/此前”。',
        'unsupported_statement_identity': '行动条件是未来假设，必须用 INFERENCE/GAP，不能标成 FACT。',
    }
    detail = guidance.get(error.code, '请按 schema 修正；不要新增证据或数字。')
    return f'第{attempt}次校验失败（{error.code}）：{detail}' if attempt <= 1 else detail
