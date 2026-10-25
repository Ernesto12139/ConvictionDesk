"""Cooperative provider deadlines and a process-wide bounded sync tool pool.

Cancellation retains a worker slot until the actual operation exits. Providers
check the same cancellation signal before/after HTTP and bound native timeouts;
late results cannot be published as successful research evidence.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
import threading
from time import monotonic

import httpx


@dataclass
class ToolDeadline:
    expires: float
    cancelled: threading.Event


_current = ContextVar('research_tool_deadline', default=None)
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='research-tool')
_slots = threading.BoundedSemaphore(4)


def tool_seconds(name):
    """SEC may need several bounded HTTP reads; local work should fail promptly."""
    if name.startswith('sec.'):
        return 65
    if name.startswith(('market.','news.')):
        return 15
    return 5


def check_tool_deadline():
    state = _current.get()
    if state and (state.cancelled.is_set() or monotonic() >= state.expires):
        raise TimeoutError('Research tool deadline exhausted')


def provider_timeout(default):
    check_tool_deadline()
    state = _current.get()
    configured = httpx.Timeout(default)
    if not state:
        return configured
    remaining = max(.001, state.expires-monotonic())
    return httpx.Timeout(**{phase:min(value,remaining) if value is not None else remaining
        for phase,value in configured.as_dict().items()})


def provider_get(client, url, **kwargs):
    """Check the total deadline between received chunks, including slow trickles."""
    if _current.get() is None:
        return client.get(url, **kwargs)
    kwargs['timeout'] = provider_timeout(getattr(client, 'timeout', 20))
    with client.stream('GET', url, **kwargs) as response:
        body = bytearray()
        for block in response.iter_bytes():
            check_tool_deadline()
            body.extend(block)
            if len(body) > 32*1024*1024:
                from .provider import ProviderError
                raise ProviderError('invalid_data', 'Research provider response exceeds byte limit')
        check_tool_deadline()
        # iter_bytes has decoded compression; do not decode it a second time.
        headers=[(key,value) for key,value in response.headers.multi_items()
                 if key.lower() not in {'content-encoding','content-length','transfer-encoding'}]
        return httpx.Response(response.status_code, headers=headers, content=bytes(body),
            request=response.request, extensions=response.extensions)


async def run_bounded_tool(operation, *, seconds):
    if not _slots.acquire(blocking=False):
        from .provider import ProviderError
        raise ProviderError('loop_tool_budget_busy', 'Research tool capacity is occupied')
    state = ToolDeadline(monotonic()+seconds, threading.Event())
    context = copy_context()
    def worker():
        token = _current.set(state)
        try:
            check_tool_deadline()
            result = operation()
            check_tool_deadline()
            return result
        finally:
            _current.reset(token)
    try:
        future = _pool.submit(context.run, worker)
    except BaseException:
        _slots.release()
        raise
    future.add_done_callback(lambda _: _slots.release())
    try:
        async with asyncio.timeout(seconds):
            return await asyncio.wrap_future(future)
    finally:
        state.cancelled.set()
