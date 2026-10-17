"""Durable pre-send reservations shared by explicit, isolated API clients."""
import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import httpx

from .llm import LLMError


class CallBudget:
    def __init__(self, path, *, authorization, ceiling='10', max_requests=40, max_models=12):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ceiling = Decimal(ceiling)
        if not self.ceiling.is_finite() or self.ceiling <= 0 or not 1 <= max_models <= max_requests:
            raise ValueError('Invalid finite API budget')
        settings = json.dumps({'authorization': authorization, 'ceiling': str(self.ceiling),
            'max_requests': max_requests, 'max_models': max_models}, sort_keys=True)
        with self.connect() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS budget_settings (id INTEGER PRIMARY KEY, value TEXT);'
                'CREATE TABLE IF NOT EXISTS api_reservations (id INTEGER PRIMARY KEY, kind TEXT, model TEXT, '
                'reserved TEXT, request_hash TEXT, status TEXT, usage TEXT);')
            prior = db.execute('SELECT value FROM budget_settings WHERE id=1').fetchone()
            if prior and prior[0] != settings:
                raise ValueError('Existing authorization limits cannot be reset')
            db.execute('INSERT OR IGNORE INTO budget_settings VALUES (1,?)', (settings,))
        self.max_requests, self.max_models = max_requests, max_models

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def reserve(self, kind, model, amount, request_hash):
        amount = Decimal(amount)
        if not amount.is_finite() or amount < 0:
            raise LLMError('api_budget_price_unknown', 'Reservation needs a known nonnegative price')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT kind,reserved FROM api_reservations').fetchall()
            if len(rows) >= self.max_requests or kind == 'model' and sum(row[0] == 'model' for row in rows) >= self.max_models:
                raise LLMError('api_budget_count', 'Authorized request count exhausted')
            if sum((Decimal(row[1]) for row in rows), Decimal(0))+amount > self.ceiling:
                raise LLMError('api_budget_cost', 'Worst-case reservation exceeds authorization')
            return db.execute('INSERT INTO api_reservations(kind,model,reserved,request_hash,status) '
                "VALUES (?,?,?,?,'RESERVED_BEFORE_SEND')", (kind, model, str(amount), request_hash)).lastrowid

    def record(self, request_id, status, usage=None):
        with self.connect() as db:
            db.execute('UPDATE api_reservations SET status=?,usage=? WHERE id=?',
                       (status, json.dumps(usage or {}), request_id))

    def summary(self):
        with self.connect() as db:
            rows = db.execute('SELECT id,kind,model,reserved,request_hash,status,usage FROM api_reservations ORDER BY id').fetchall()
        reserved = sum((Decimal(row[3]) for row in rows), Decimal(0))
        return {'request_count': len(rows), 'model_count': sum(row[1] == 'model' for row in rows),
            'max_requests': self.max_requests, 'max_models': self.max_models,
            'reserved_usd': str(reserved), 'remaining_reservation_usd': str(self.ceiling-reserved),
            'billing_verified': False, 'requests': [dict(zip(
                ('id','kind','model','reserved_usd','request_hash','status','usage'),
                (*row[:6], json.loads(row[6] or '{}')))) for row in rows]}


class BudgetedModelTransport(httpx.AsyncBaseTransport):
    """Known direct prices only; no tools, redirects or hidden retry refund."""
    def __init__(self, inner, budget):
        self.inner, self.budget, self.stopped = inner, budget, False

    async def handle_async_request(self, request):
        if self.stopped:
            raise LLMError('api_budget_stopped', 'Previous uncertain response stops automatic sends')
        try:
            body = json.loads(request.content)
        except ValueError:
            raise LLMError('api_budget_configuration', 'Expected explicit JSON model request') from None
        url, model = str(request.url), body.get('model')
        wire = request.content
        input_upper = len(wire)+2048
        if request.method != 'POST' or len(wire) > 65536:
            raise LLMError('api_budget_configuration', 'Unexpected method or oversized model input')
        if url == 'https://api.openai.com/v1/responses' and model == 'gpt-6.1-sol':
            if set(body)-{'model','store','reasoning','max_output_tokens','input','text','service_tier'}:
                raise LLMError('api_budget_configuration','Only reviewed text-generation fields are allowed')
            if 'input' in body and (not isinstance(body['input'],list) or any(
                    not isinstance(row,dict) or set(row)-{'role','content'} or not isinstance(row.get('content'),str)
                    for row in body['input'])):
                raise LLMError('api_budget_configuration','Only inline text input has a verified token bound')
            maximum = body.get('max_output_tokens')
            if (body.get('service_tier', 'default') != 'default' or body.get('store') is not False
                    or body.get('tools') or body.get('previous_response_id')
                    or type(maximum) is not int or not 256 <= maximum <= 8192):
                raise LLMError('api_budget_configuration', 'Unapproved model tier, tools or output ceiling')
            body['service_tier'] = 'default'
            wire = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
            input_upper = len(wire)+2048
            reserve = (Decimal(input_upper)*Decimal('2.50')+Decimal(maximum)*Decimal('10'))/1_000_000
            rate_in, rate_out = Decimal('2'), Decimal('10')
        elif url == 'https://api.typesafe.ai/v1/systemone' and model in {'jev-1.13.0', 'jev-1.14.0'}:
            if (set(body)-{'model','state','questions'} or len(wire) > 32000
                    or 'TypeSafe-Danger-Zone-Long-Context' in request.headers):
                raise LLMError('api_budget_configuration', 'Jev input exceeds short-context budget')
            # Conservatively allow the full wire bound once per question.
            input_upper *= max(1, len(body.get('questions', {})))
            reserve = Decimal(input_upper)*Decimal('.042')/1_000_000
            rate_in, rate_out = Decimal('.042'), Decimal(0)
        else:
            raise LLMError('api_budget_price_unknown', 'Endpoint/model has no verified price')
        digest = hashlib.sha256(wire).hexdigest()
        request_id = self.budget.reserve('model', model, reserve, digest)
        forwarded = httpx.Request('POST', url, headers=request.headers, content=wire, extensions=request.extensions)
        forwarded.headers['content-length'] = str(len(wire))
        try:
            response = await self.inner.handle_async_request(forwarded)
            await response.aread()
            envelope = response.json()
            usage = envelope.get('usage', {})
            reported_model = envelope.get('model', model)
            if reported_model != model:
                raise LLMError('api_budget_identity', 'Response model differs from priced model')
            i, o = usage.get('input_tokens'), usage.get('output_tokens', 0)
            if type(i) is not int or i < 0 or i > input_upper or type(o) is not int or o < 0:
                raise LLMError('api_budget_usage_unknown', 'Usage is absent or exceeds reservation')
            if model == 'gpt-6.1-sol' and o > maximum:
                raise LLMError('api_budget_usage_unknown', 'Output exceeded configured maximum')
            self.budget.record(request_id, 'RETURNED_USAGE_KNOWN',
                {'input_tokens': i, 'output_tokens': o, 'http_status': response.status_code,
                 'standard_rate_estimate_usd': str((i*rate_in+o*rate_out)/1_000_000)})
            if response.status_code != 200:
                self.stopped = True
            return response
        except BaseException:
            self.stopped = True
            self.budget.record(request_id, 'FAILED_FULL_RESERVATION_RETAINED')
            raise

    async def aclose(self):
        await self.inner.aclose()
