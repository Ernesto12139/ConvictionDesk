"""Pinned optional Jev-Mem retrieval over a prefiltered SQLite memory view.

No credential/SDK construction on the production path: the caller supplies an
admitted upstream-compatible client. Building the disposable graph uses the
upstream's explicit offline mock, with no LLM, retry, download or save/load.
The lexical encoder is labelled honestly; a verified local encoder is optional.
"""
from __future__ import annotations

import hashlib
import importlib
import sys
from pathlib import Path

from .persistent_research_memory import (
    MemorySelectionDecision, MemorySelectionRequest, UPSTREAM_COMMIT, UPSTREAM_SOURCE_SHA256,
)


def load_pinned_jev_mem(folder: str | Path) -> dict:
    folder = Path(folder).resolve()
    digest = hashlib.sha256()
    for path in sorted(folder.rglob('*.py')):
        digest.update(path.relative_to(folder).as_posix().encode())
        digest.update(path.read_bytes().replace(b'\r\n', b'\n'))
    if digest.hexdigest() != UPSTREAM_SOURCE_SHA256:
        raise ValueError('Jev-Mem pinned upstream source binding mismatch')
    # A previous import of another package named memory must never win sys.path.
    for name, module in tuple(sys.modules.items()):
        if name == 'memory' or name.startswith('memory.'):
            origin = getattr(module, '__file__', None)
            if origin is None or not Path(origin).resolve().is_relative_to(folder):
                raise ValueError('unexpected preloaded Jev-Mem module identity')
    sys.path.insert(0, str(folder))
    try:
        classes = {
            'graph': ('memory.graph_db', 'NetworkXGraphDB'),
            'encoder': ('memory.mock_encoder', 'MockEncoder'),
            'builder': ('memory.memory_builder', 'MemoryBuilder'),
            'query': ('memory.query_engine', 'QueryEngine'),
            'config': ('memory.jev_mem_config', 'JevMemConfig'),
            'client': ('memory.jev_client', 'JevClient'),
            'trg': ('memory.trg_memory', 'TemporalResonanceGraphMemory'),
            'vector': ('memory.vector_db', 'NumpyVectorDB'),
        }
        result = {}
        for key, (name, attribute) in classes.items():
            module = importlib.import_module(name)
            if not Path(module.__file__).resolve().is_relative_to(folder):
                raise ValueError('unexpected Jev-Mem module origin')
            result[key] = getattr(module, attribute)
        return result
    finally:
        sys.path.remove(str(folder))


class PinnedJevMemSelector:
    """Callable for PersistentResearchMemory.recall(selector=...).

The injected client must implement upstream evaluate/probabilities,
retrieval_slot and audit.emit, route all sends through Root admission/budget,
and keep the stable request.research_task_id (never key budgets by purpose).
No client is created by default, and no fallback client can send implicitly.
"""

    def __init__(self, upstream_root: str | Path, *, admitted_client, trace_id: str,
                 encoder=None, maximum_jev_calls: int = 3, offline_mock: bool = False):
        if admitted_client is None or not trace_id or not 1 <= maximum_jev_calls <= 10:
            raise ValueError('explicit admitted client, trace and finite call bound required')
        self.api = load_pinned_jev_mem(upstream_root)
        self.client = admitted_client
        self.trace_id = trace_id
        self.encoder = encoder or self.api['encoder']()
        self.maximum_jev_calls = maximum_jev_calls
        self.offline_mock = offline_mock
        self.last_trace: dict = {}

    def __call__(self, request: MemorySelectionRequest) -> MemorySelectionDecision:
        if len(request.candidates) > 48 or any(x.rights == 'LOCAL_ONLY' for x in request.candidates):
            raise ValueError('Jev-Mem requires bounded prefiltered model-egress memory')
        api = self.api
        graph = api['trg'](graph_db=api['graph'](), vector_db=api['vector'](self.encoder.dimension),
                           encoder=self.encoder, llm_backend=None)
        offline_config = api['config'](write_enabled=True, read_enabled=False, jev_mock=True,
            admission_enabled=False, max_retries=0, consolidation_interval=0, audit_path='')
        offline_client = api['client'](offline_config, api_key='')
        # The builder never saves; this inert path is not created or read.
        builder = api['builder']('__unused_derived_memory__', jev_config=offline_config,
            jev_client=offline_client, trg_memory=graph, llm_enabled=False)
        by_node = {}
        for item in request.candidates:
            narrative = f'[MEMORY {item.record.identity}; not original source evidence] {item.record.summary}'
            node = builder.build(narrative, timestamp=item.record.as_of,
                metadata={'symbol': item.record.symbol, 'identity': item.record.identity,
                          'record_id': item.record.record_id, 'evidence_ids': item.evidence_ids,
                          'causal_links_are_hypotheses': True})
            if node is not None:
                by_node[node.node_id] = item.record.record_id
        read_config = api['config'](write_enabled=False, read_enabled=True,
            jev_mock=self.offline_mock, max_retries=0, consolidation_interval=0,
            maximum_jev_calls=self.maximum_jev_calls, maximum_depth=2,
            maximum_nodes=min(48, max(1, len(by_node))), maximum_edges=96,
            candidate_top_k=min(24, max(1, len(by_node))), fallback_to_magma=False, audit_path='')
        context, _ = api['query'](graph, builder.node_index, jev_config=read_config,
                                   jev_client=self.client).query(request.question, top_k=request.max_records)
        selected = tuple(dict.fromkeys(by_node[node.node_id] for node in context.anchor_nodes
                                       if node.node_id in by_node))[:request.max_records]
        metadata = dict(context.metadata)
        stop = metadata.get('stopping_decision', 'UNKNOWN')
        self.last_trace = {'upstream_commit': UPSTREAM_COMMIT,
            'upstream_source_sha256': UPSTREAM_SOURCE_SHA256,
            'mode': 'OFFLINE_MOCK' if self.offline_mock else 'ROOT_INJECTED_JEV',
            'research_task_id': request.research_task_id, 'purpose': request.purpose,
            'encoder': 'UPSTREAM_LEXICAL_MOCK' if type(self.encoder).__name__ == 'MockEncoder' else 'CALLER_LOCAL_ENCODER',
            'causal_links': 'HYPOTHESES_NOT_PROVEN_CAUSALITY', **metadata}
        return MemorySelectionDecision(selected_ids=selected,
            sufficient=stop == 'evidence_sufficient', reason_code=str(stop), trace_id=self.trace_id)
