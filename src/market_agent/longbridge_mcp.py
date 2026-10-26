"""Bounded read-only Longbridge MCP, separate from portfolio and EOD truth.

Streamable HTTP JSON-RPC POST supports JSON and SSE responses. OAuth/Agent Auth
Code credentials must belong to this client; no Codex connector token reuse.
"""
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from threading import RLock
from time import perf_counter

import httpx

from .clock import system_utc_now
from .decision_context import MarketReference
from .domain import EvidenceItem, utc
from .provider import SYMBOL, ProviderError
from .tdb import content_hash

ENDPOINT = 'https://mcp.longbridge.com'
READ_TOOLS = frozenset({'quote', 'static_info', 'trading_days', 'trading_session'})
PROTOCOLS = {'2025-03-26', '2025-06-18', '2025-11-25'}


class LongbridgeMCPClient:
    """No generic tool execution, subscriptions, hidden retries or trading tools."""
    def __init__(self, token='', *, client=None, authorization_channel=False):
        self._token = token
        self.client = client or httpx.Client(timeout=15, follow_redirects=False)
        self.endpoint = ENDPOINT + ('/agent' if authorization_channel else '')
        self.authorization_channel = authorization_channel
        self.protocol = None
        self._session = None
        self._next_id = 0
        self._lock = RLock()
        self.server_info = {}
        self.tools = {}

    @contextmanager
    def _operation(self, deadline=None):
        if not self._lock.acquire(timeout=.25):
            raise ProviderError('mcp_busy','Optional MCP client is busy')
        try:
            yield deadline if deadline is not None else perf_counter()+20
        finally:
            self._lock.release()

    def _rpc(self, method, params=None, *, notification=False, deadline):
        self._next_id += 1
        request_id = self._next_id
        payload = {'jsonrpc':'2.0', 'method':method}
        if not notification:
            payload['id'] = request_id
        if params is not None:
            payload['params'] = params
        headers = {'Accept':'application/json, text/event-stream'}
        if self._token:
            headers['Authorization'] = 'Bearer ' + self._token
        if self._session:
            headers['MCP-Session-Id'] = self._session
        if self.protocol:
            headers['MCP-Protocol-Version'] = self.protocol
        def remaining():
            from .tool_deadline import check_tool_deadline
            check_tool_deadline()
            seconds=deadline-perf_counter()
            if seconds<=0:raise ProviderError('timeout','MCP total operation deadline exceeded')
            return seconds
        try:
            seconds=remaining()
            from .tool_deadline import provider_timeout
            timeout=provider_timeout(min(seconds,5))
            with self.client.stream('POST', self.endpoint, headers=headers, json=payload, timeout=timeout) as response:
                if response.status_code in {401,403}:
                    raise ProviderError('CONNECTOR_PENDING' if not self._token else 'authentication', 'MCP authorization required')
                if response.status_code != 200 and not (notification and response.status_code == 202):
                    code = 'rate_limit' if response.status_code == 429 else 'provider_unavailable' if response.status_code >= 500 else 'mcp_protocol'
                    raise ProviderError(code, 'MCP request rejected')
                if notification:
                    return None
                if method == 'initialize':
                    self._session = response.headers.get('mcp-session-id')
                content_type = response.headers.get('content-type','').split(';')[0]
                if content_type == 'text/event-stream':
                    data, count, buffer, events = [], 0, '', 0
                    # Check each received chunk, even when no newline ever arrives.
                    # iter_lines alone permits an endless incomplete-line trickle.
                    for block in response.iter_text():
                        remaining();count+=len(block.encode('utf-8'));buffer+=block
                        if count > 8*1024*1024:
                            raise ProviderError('invalid_data', 'MCP response too large')
                        while '\n' in buffer:
                            line,buffer=buffer.split('\n',1);line=line.rstrip('\r');events+=1
                            if events>1024:raise ProviderError('timeout','MCP stream event budget exhausted')
                            if line.startswith('data:'):
                                data.append(line[5:].lstrip())
                            elif not line and data:
                                raw = json.loads('\n'.join(data)); data = []
                                if isinstance(raw,dict) and raw.get('id') == request_id:
                                    return self._result(raw, request_id)
                    raise ProviderError('mcp_protocol', 'MCP response missing matching result')
                if content_type != 'application/json':
                    raise ProviderError('mcp_protocol', 'MCP content type unsupported')
                body = bytearray()
                for block in response.iter_bytes():
                    remaining()
                    body.extend(block)
                    if len(body) > 8*1024*1024:
                        raise ProviderError('invalid_data', 'MCP response too large')
                return self._result(json.loads(body), request_id)
        except httpx.TimeoutException:
            raise ProviderError('timeout', 'MCP request timed out') from None
        except httpx.RequestError:
            raise ProviderError('transport', 'MCP request unavailable') from None
        except (ValueError, TypeError):
            raise ProviderError('invalid_data', 'MCP result invalid') from None

    @staticmethod
    def _result(raw, request_id):
        if not isinstance(raw,dict) or raw.get('jsonrpc') != '2.0' or raw.get('id') != request_id:
            raise ProviderError('mcp_protocol', 'MCP response identity mismatch')
        if 'error' in raw:
            raise ProviderError('mcp_protocol', 'MCP returned an error')
        if not isinstance(raw.get('result'),dict):
            raise ProviderError('invalid_data', 'MCP result must be an object')
        return raw['result']

    def discover(self, *, deadline=None):
        with self._operation(deadline) as deadline:
            if not self.protocol:
                result = self._rpc('initialize', {'protocolVersion':'2025-11-25', 'capabilities':{},
                    'clientInfo':{'name':'mia-readonly-research', 'version':'0.1'}},deadline=deadline)
                if result.get('protocolVersion') not in PROTOCOLS or not isinstance(result.get('serverInfo'),dict):
                    raise ProviderError('mcp_protocol', 'MCP negotiation unsupported')
                self.protocol = result['protocolVersion']
                self.server_info = {key:result['serverInfo'].get(key) for key in ('name','version')}
                self._rpc('notifications/initialized', notification=True,deadline=deadline)
            tools, cursor = {}, None
            for _ in range(5):
                result = self._rpc('tools/list', {'cursor':cursor} if cursor else {},deadline=deadline)
                rows = result.get('tools')
                if not isinstance(rows,list) or any(not isinstance(row,dict) or not isinstance(row.get('name'),str) for row in rows):
                    raise ProviderError('invalid_data', 'MCP tool catalog invalid')
                for row in rows:
                    if row['name'] in READ_TOOLS or self.authorization_channel and row['name'] == 'authenticate':
                        tools[row['name']] = row
                cursor = result.get('nextCursor')
                if not cursor:
                    self.tools = tools
                    return list(tools)
                if not isinstance(cursor,str):
                    raise ProviderError('invalid_data', 'MCP catalog cursor invalid')
            raise ProviderError('mcp_protocol', 'MCP catalog exceeds discovery budget')

    @staticmethod
    def tool_data(result):
        if result.get('isError'):
            # Tool bodies may contain private/provider prose; never log them.
            raise ProviderError('mcp_tool_error', 'MCP read-only tool failed')
        if 'structuredContent' in result:
            return result['structuredContent']
        parts = result.get('content')
        if not isinstance(parts,list) or len(parts) != 1 or not isinstance(parts[0],dict) or parts[0].get('type') != 'text':
            raise ProviderError('invalid_data', 'MCP tool data unavailable')
        try:
            return json.loads(parts[0]['text'], parse_float=Decimal)
        except (KeyError, ValueError, TypeError):
            raise ProviderError('invalid_data', 'MCP tool JSON invalid') from None

    def call(self, name, arguments):
        # Guard BEFORE discovery/network, even if server advertises trading.
        if name not in READ_TOOLS or self.authorization_channel:
            raise ProviderError('tool_not_allowed', 'Only approved public read-only research tools are allowed')
        with self._operation() as deadline:
            if not self.protocol:
                self.discover(deadline=deadline)
            if name not in self.tools:
                raise ProviderError('not_entitled', 'Read-only tool is not available in current MCP scope')
            return self.tool_data(self._rpc('tools/call', {'name':name,'arguments':arguments},deadline=deadline))

    def authenticate(self, auth_code):
        """Explicit one-time exchange on /agent; token returned only to the caller."""
        if not self.authorization_channel or not auth_code.strip():
            raise ProviderError('invalid_input', 'Explicit authorization code channel required')
        with self._operation() as deadline:
            self.discover(deadline=deadline)
            if 'authenticate' not in self.tools:
                raise ProviderError('CONNECTOR_PENDING', 'Authorization tool unavailable')
            return self.tool_data(self._rpc('tools/call', {'name':'authenticate','arguments':{'auth_code':auth_code}},deadline=deadline))


class LongbridgeMCPQuoteProvider:
    name = 'longbridge_mcp'
    def __init__(self, mcp, *, clock=system_utc_now):
        self.mcp, self.clock = mcp, clock

    def reference(self, symbol, now):
        cutoff = utc(now)
        if not SYMBOL.fullmatch(symbol):
            return None, None, 'INVALID_INPUT'
        try:
            payload = self.mcp.call('quote', {'symbols':[symbol+'.US']})
            rows = payload.get('data', payload.get('quotes')) if isinstance(payload,dict) else payload
            if not isinstance(rows,list) or len(rows) != 1:
                raise ValueError('missing quote')
            row = rows[0]
            if not isinstance(row,dict) or row.get('symbol') != symbol+'.US' or isinstance(row.get('last_done'),bool):
                raise ValueError('quote identity mismatch')
            price = Decimal(str(row.get('last_done')))
            timestamp = utc(datetime.fromisoformat(row['timestamp'].replace('Z','+00:00')))
            retrieved = utc(self.clock())
            if not price.is_finite() or price <= 0 or timestamp > cutoff or timestamp > retrieved:
                raise ValueError('quote value/PIT invalid')
            if cutoff-timestamp > timedelta(days=7):
                return None, None, 'STALE'
            # Do not mix overnight/pre/post values with the regular quote timestamp.
            normalized = {'symbol':symbol,'price':str(price),'currency':'USD','as_of':timestamp.isoformat(),
                'provider':self.name,'basis':'QUOTE_CURRENT','session':'REGULAR',
                'account_scope':'PUBLIC_MARKET','mcp_tool':'quote','trade_status':row.get('trade_status'),
                'entitlement':'OBSERVED_QUOTE_ONLY','retrieved_at':retrieved.isoformat()}
            digest = content_hash(normalized); eid = 'E'+digest[7:23]
            reference = MarketReference(price=price,as_of=timestamp,retrieved_at=retrieved,
                provider=self.name,evidence_id=eid,basis='QUOTE_CURRENT',session='REGULAR')
            evidence = EvidenceItem(evidence_id=eid,evidence_type='market_data',provider=self.name,symbol=symbol,
                title='Longbridge MCP 常规时段报价参考',source_uri='https://open.longbridge.com/docs/mcp',
                as_of=timestamp,retrieved_at=retrieved,content_hash=digest,structured_data=normalized)
            return reference, evidence, 'AVAILABLE'
        except ProviderError as exc:
            return None, None, exc.code
        except (ValueError, TypeError, KeyError, InvalidOperation):
            return None, None, 'INVALID_DATA'


def session_quote_evidence(row, *, symbol, cutoff, retrieved_at):
    """Normalize observed optional sessions separately; missing never becomes zero.

    Can consume native MCP or an explicitly captured connected-tool public quote.
    This does not claim account-level subscription or enable overnight remotely.
    """
    cutoff, retrieved_at = utc(cutoff), utc(retrieved_at)
    if not isinstance(row, dict) or row.get('symbol') != symbol+'.US':
        raise ProviderError('invalid_data', 'Quote symbol mismatch')
    results, unavailable = [], []
    for session, keys in [('REGULAR',()),('PRE_MARKET',('pre_market_quote','pre_market')),
            ('POST_MARKET',('post_market_quote','post_market')),('OVERNIGHT',('overnight_quote','overnight'))]:
        data = row if not keys else next((row[k] for k in keys if isinstance(row.get(k),dict)),None)
        if not data:
            unavailable.append(session);continue
        try:
            value, stamp = data.get('last_done'), data.get('timestamp')
            if isinstance(value,bool) or isinstance(stamp,bool):raise ValueError('invalid numeric type')
            price=Decimal(str(value))
            timestamp=(datetime.fromtimestamp(int(stamp),timezone.utc) if str(stamp).isdigit()
                else utc(datetime.fromisoformat(stamp.replace('Z','+00:00'))))
            if not price.is_finite() or price<=0 or timestamp>cutoff or timestamp>retrieved_at:
                raise ValueError('quote value/PIT')
        except (ValueError,TypeError,AttributeError,OverflowError,InvalidOperation):
            unavailable.append(session);continue
        payload={'symbol':symbol,'price':str(price),'currency':'USD','session':session,
            'as_of':timestamp.isoformat(),'retrieved_at':retrieved_at.isoformat(),
            'entitlement':'OBSERVED_SESSION_FIELD_ONLY','basis':'QUOTE_CURRENT'}
        digest=content_hash(payload)
        results.append(EvidenceItem(evidence_id='E'+digest[7:23],evidence_type='market_data',
            provider='longbridge_mcp',symbol=symbol,title='Longbridge '+session+' quote',
            source_uri='https://open.longbridge.com/docs/quote/pull/quote',as_of=timestamp,
            retrieved_at=retrieved_at,content_hash=digest,structured_data=payload))
    return results, unavailable
