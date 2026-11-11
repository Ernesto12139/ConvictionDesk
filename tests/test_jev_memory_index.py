"""Actual pinned upstream graph/query in OFFLINE_MOCK mode; never live evidence."""
import os
import socket
from datetime import datetime, timezone

import pytest

from market_agent.adaptive_memory import MemoryRecord
from market_agent.jev_memory_index import PinnedJevMemSelector, load_pinned_jev_mem
from market_agent.persistent_research_memory import MemorySelectionRequest, StoredMemory


def test_source_binding_rejects_untrusted_code_before_import(tmp_path):
    (tmp_path / 'hostile.py').write_text('raise AssertionError("must never import")')
    with pytest.raises(ValueError, match='source binding'):
        load_pinned_jev_mem(tmp_path)


@pytest.mark.skipif(not os.environ.get('MIA_JEV_MEM_UPSTREAM'), reason='optional pinned artifact not configured')
def test_actual_upstream_scoped_lexical_graph_query_without_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('external request forbidden in offline Jev-Mem gate')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    folder = os.environ['MIA_JEV_MEM_UPSTREAM']
    api = load_pinned_jev_mem(folder)
    config = api['config'](jev_mock=True, max_retries=0, audit_path='')
    client = api['client'](config, api_key='')
    selector = PinnedJevMemSelector(folder, admitted_client=client,
                                   trace_id='OFFLINE_MOCK-1', offline_mock=True)
    first = datetime(2026, 11, 1, tzinfo=timezone.utc)
    candidates = tuple(StoredMemory(tenant_id='synthetic-tenant', source_task_id=f'task-{i}',
        record=MemoryRecord(record_id=f'r-{i}', kind='THESIS', identity='AI_DRAFT', symbol='APP',
            summary=summary, as_of=first), rights='SYNTHETIC', stored_at=first)
        for i, summary in enumerate(('APP cash collections support the prior thesis',
                                     'APP weak cash collections challenge the prior thesis')))
    result = selector(MemorySelectionRequest(research_task_id='task-B', question='APP cash collections',
                                             candidates=candidates, max_records=2))
    assert result.selected_ids and set(result.selected_ids) <= {'r-0', 'r-1'}
    assert selector.last_trace['mode'] == 'OFFLINE_MOCK'
    assert selector.last_trace['encoder'] == 'UPSTREAM_LEXICAL_MOCK'
    assert selector.last_trace['research_task_id'] == 'task-B'
    assert selector.last_trace['jev_calls'] <= 3
    assert selector.last_trace['causal_links'] == 'HYPOTHESES_NOT_PROVEN_CAUSALITY'
    with pytest.raises(ValueError, match='model-egress'):
        selector(MemorySelectionRequest(research_task_id='task-B', question='cash', max_records=2,
            candidates=(candidates[0].model_copy(update={'rights': 'LOCAL_ONLY'}),)))
