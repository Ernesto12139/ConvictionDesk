"""Explicit, short-lived provider rate qualification. USD/CNY per million tokens."""
from datetime import datetime, timezone
from decimal import Decimal
from .llm import LLMError

RATE_CARD = {
    # Re-verified 2026-10-23 against the same official pages (values unchanged):
    # output/convergence-002/budget/PRICING_RECHECK_20261023.json
    'id':'official-2026-10-23-peak-upper-v2',
    'verified_at':'2026-10-23T02:52:06.067317+00:00',
    'expires_at':'2026-10-24T23:59:59+00:00',
    'deepseek_source':'https://api-docs.deepseek.com/quick_start/pricing/',
    'deepseek_cny_source':'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
    'openai_source':'https://developers.openai.com/api/docs/models/gpt-6.1-sol',
    'deepseek_model_version':'DeepSeek-V4.1-Flash-1017',
    'deepseek_usd':{'input':'0.3','output':'1.2'},
    'deepseek_cny':{'input':'2','output':'8'},
    'openai_usd':{'input':'2','output':'10'},
    'scope':'Peak, uncached upper estimate. No cache discount or FX conversion. Reverify before expiry; not perpetual prices.'}


def qualified_rates(card, *, now=None):
    now = now or datetime.now(timezone.utc)
    if card != RATE_CARD or not datetime.fromisoformat(card['verified_at']) <= now <= datetime.fromisoformat(card['expires_at']):
        raise LLMError('api_budget_price_expired','Explicit current reviewed rate card required')
    return Decimal(card['deepseek_usd']['input']),Decimal(card['deepseek_usd']['output'])
