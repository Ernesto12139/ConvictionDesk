"""Opt-in execution-bound observations of the existing research service.

No history reconstruction, payload logging, provider dispatch or new persistence.
The trusted installer supplies a source identity and a local allowlist-only sink.
"""
from contextlib import contextmanager
from datetime import datetime

from .agent import MockLLMClient
from .run_trace import ModelInvocation, SourceRef, TokenState, TraceRecorder, export_trace
from .tdb import content_hash


def source_refs(sources, cutoff):
    refs = []
    for source in sources:
        evidence = source.evidence
        accepted = evidence.structured_data.get('accepted_at')
        if isinstance(accepted, str):
            try:
                accepted = datetime.fromisoformat(accepted.replace('Z', '+00:00'))
            except ValueError:
                accepted = None
        if not isinstance(accepted, datetime) or accepted.tzinfo is None:
            accepted = None
        times = [v for v in (evidence.retrieved_at, evidence.published_at, evidence.as_of, accepted) if v is not None]
        refs.append(SourceRef(source_id=evidence.evidence_id, kind=evidence.provider,
                              accepted_at=accepted, captured_at=evidence.retrieved_at,
                              available_at=max(times) if times else None,
                              content_hash=evidence.content_hash, rights='UNKNOWN',
                              pit_ok=all(v <= cutoff for v in times) if times else None))
    return refs


class ResearchExecutionTrace:
    def __init__(self, *, code, sink, execution_mode):
        self.code, self.sink, self.execution_mode = code, sink, execution_mode

    def start(self, task, operation='research'):
        return TraceRecorder(run_id=f'{task.task_id}:r{task.revision}:{operation}',
                             task_id=task.task_id, revision_id=str(task.revision),
                             execution_mode=self.execution_mode, code=self.code,
                             as_of=task.cutoff, pit_cutoff=task.cutoff)

    def finish(self, recorder, task):
        complete = task.status == 'COMPLETE'
        recorder.record_gate('CONTRACT', decision='ALLOW' if complete else 'DENY',
                             code='REVISION_COMMITTED' if complete else task.error or task.status)
        status = 'COMPLETE' if complete else 'BLOCKED' if task.status == 'NEEDS_INPUT' else 'FAILED'
        run = recorder.close(status=status, error_code=task.error or (None if complete else task.status))
        self.sink(export_trace(run))

    async def tool(self, recorder, name, current_task, operation):
        before = len(current_task().tool_calls)
        with recorder.record_tool(name) as span:
            result = await operation()
            task = current_task()
            if len(task.tool_calls) > before:
                call = task.tool_calls[-1]
                span.evidence_ids = list(call.output_refs)
                span.sources = [ref for ref in source_refs(task.sources, task.cutoff)
                                if ref.source_id in call.output_refs]
                if call.status != 'success':
                    span.status = 'ERROR'
                    recorder.record_gate('CONTRACT', decision='DENY', code=call.error or 'TOOL_FAILED')
            span.output_count = len(result) if isinstance(result, (list, tuple)) else (0 if result is None else 1)
            return result

    @contextmanager
    def model(self, recorder, client, purpose, sources, cutoff):
        scripted = isinstance(client, MockLLMClient)
        if self.execution_mode == 'KEYLESS_SCRIPTED' and not scripted:
            raise ValueError('TRACE_EXECUTION_MODE_MISMATCH')
        with recorder.span('model', purpose, input_class={
                'provider': getattr(client, 'provider', 'UNKNOWN'),
                'model': client.model_name, 'purpose': purpose}) as span:
            state = (TokenState.not_applicable('scripted adapter, no provider dispatch')
                     if scripted else TokenState.unknown('provider usage not measured'))
            span.model = ModelInvocation(execution='SCRIPTED' if scripted else 'REAL_MODEL',
                                         provider=getattr(client, 'provider', None),
                                         model=client.model_name, purpose=purpose,
                                         input_tokens=state, output_tokens=state,
                                         cost=state if scripted else TokenState.unknown('invoice not reconciled'))
            span.sources = source_refs(sources, cutoff)
            span.evidence_ids = [source.evidence.evidence_id for source in sources]
            if not scripted:
                span.notes = ['ADAPTER_ATTEMPT_TRANSPORT_NOT_ASSERTED']
            yield span
            if not scripted:
                metadata = getattr(client, 'call_metadata', lambda: {})()
                usage = metadata.get('usage', {})
                changes = {}
                for key in ('input_tokens', 'output_tokens'):
                    value = usage.get(key)
                    if type(value) is int and value >= 0:
                        changes[key] = TokenState.known(value, 'tokens')
                span.model = span.model.model_copy(update=changes)

    def whatif(self, task, operation):
        recorder = self.start(task, 'whatif')
        try:
            with recorder.record_tool('whatif.decimal') as span:
                result = operation()
                payload = result.model_dump(mode='json') if hasattr(result, 'model_dump') else result
                span.output_count = 1
                span.notes = ['HYPOTHETICAL_RECEIPT_ONLY', content_hash(payload)]
            run = recorder.close(status='COMPLETE')
        except BaseException as error:
            run = recorder.close(status='FAILED', error_code=getattr(error, 'code', type(error).__name__))
            self.sink(export_trace(run))
            raise
        self.sink(export_trace(run))
        return result


def install_execution_trace(app, observer):
    """Trusted local composition only; no API or environment opt-in is accepted."""
    app.state.research_service.execution_observer = observer
