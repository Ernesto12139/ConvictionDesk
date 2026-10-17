import asyncio
from decimal import Decimal

import httpx
import pytest

from market_agent.call_budget import BudgetedModelTransport, CallBudget
from market_agent.llm import LLMError


def test_reservations_survive_reopen_and_share_provider_ceiling(tmp_path):
    path = tmp_path/'budget.sqlite3'
    ledger = CallBudget(path, authorization='synthetic', ceiling='1', max_requests=3, max_models=2)
    ledger.reserve('data', 'included-free-source', '0', 'hash')
    ledger.reserve('model', 'gpt-6.1-sol', '.6', 'hash')
    reopened = CallBudget(path, authorization='synthetic', ceiling='1', max_requests=3, max_models=2)
    with pytest.raises(LLMError, match='api_budget_cost'):
        reopened.reserve('model', 'jev-1.13.0', '.5', 'hash')
    reopened.reserve('model', 'jev-1.13.0', '.1', 'hash')
    with pytest.raises(LLMError, match='api_budget_count'):
        reopened.reserve('data', 'free', '0', 'hash')
    with pytest.raises(ValueError):
        CallBudget(path, authorization='synthetic', ceiling='10', max_requests=3, max_models=2)


def test_unknown_usage_stops_retries_and_retains_reservation(tmp_path):
    sent = []
    async def respond(request):
        sent.append(request)
        return httpx.Response(500, json={'error': 'synthetic'})
    ledger = CallBudget(tmp_path/'budget.sqlite3', authorization='synthetic')
    transport = BudgetedModelTransport(httpx.MockTransport(respond), ledger)
    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            for expected in ('api_budget_usage_unknown', 'api_budget_stopped'):
                with pytest.raises(LLMError, match=expected):
                    await client.post('https://api.openai.com/v1/responses',
                        headers={'Authorization': 'Bearer SYNTHETIC_SECRET'},
                        json={'model':'gpt-6.1-sol','store':False,'max_output_tokens':512})
    asyncio.run(run())
    assert len(sent) == 1 and ledger.summary()['request_count'] == 1
    assert Decimal(ledger.summary()['reserved_usd']) > 0
    assert 'SYNTHETIC_SECRET' not in str(ledger.summary())


def test_unpriced_remote_inputs_and_tool_fields_are_rejected_before_send(tmp_path):
    ledger = CallBudget(tmp_path/'budget.sqlite3',authorization='synthetic')
    transport = BudgetedModelTransport(httpx.MockTransport(lambda request:pytest.fail('Unexpected send')),ledger)
    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            for fields in ({'conversation':'remote'}, {'input':[{'role':'user','content':[{'type':'input_image','image_url':'https://example.com'}]}]}):
                with pytest.raises(LLMError,match='api_budget_configuration'):
                    await client.post('https://api.openai.com/v1/responses',json={
                        'model':'gpt-6.1-sol','store':False,'max_output_tokens':512,**fields})
    asyncio.run(run())
    assert ledger.summary()['request_count']==0
