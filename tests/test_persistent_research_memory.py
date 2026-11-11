"""Synthetic-only lifecycle, tenant/egress and restart proofs; no live Jev."""
import sqlite3
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

import pytest

from market_agent.adaptive_memory import MemoryRecord, TaskMemory
from market_agent.persistent_research_memory import (
    MemoryAnnotation, MemoryQuery, MemorySelectionDecision, MemoryWriteDecision,
    PersistentResearchMemory, StoredMemory,
)


def at(day):
    return datetime(2026, 11, day, tzinfo=timezone.utc)


def memory(record_id='old', **updates):
    values = dict(tenant_id='synthetic-tenant', source_task_id='task-A',
        record=MemoryRecord(record_id=record_id, kind='THESIS', identity='AI_DRAFT',
            symbol='APP', as_of=at(1), summary='APP cash conversion appears stable'),
        rights='SYNTHETIC', stored_at=at(1))
    values.update(updates)
    return StoredMemory(**values)


def query(**updates):
    values = dict(research_task_id='task-B', tenant_id='synthetic-tenant',
        question='APP cash conversion', symbol='APP', cutoff=at(4), evaluation_time=at(5))
    values.update(updates)
    return MemoryQuery(**values)


@pytest.fixture
def store():
    connection = sqlite3.connect(':memory:')
    result = PersistentResearchMemory(connection)
    result.install_schema()
    yield result
    connection.close()


def ids(result):
    return tuple(x.record_id for x in result.records)


def test_cross_task_and_connection_restart_preserves_original_bytes(tmp_path):
    path = tmp_path / 'synthetic.sqlite'
    with sqlite3.connect(path) as connection:
        store = PersistentResearchMemory(connection)
        store.install_schema()
        original = memory()
        store.append(original)
        frozen = connection.execute('SELECT payload,sha256 FROM research_memory_v1').fetchone()
    with sqlite3.connect(path) as connection:
        store = PersistentResearchMemory(connection)
        result = store.recall(query())
        assert ids(result) == ('old',)
        assert result.source_task_ids == ('task-A',)
        assert result.records[0].identity == 'AI_DRAFT'
        assert not result.records[0].market_fact
        assert connection.execute('SELECT payload,sha256 FROM research_memory_v1').fetchone() == frozen


def test_task_a_write_task_b_fresh_process_recall(tmp_path):
    path = tmp_path / 'synthetic-process.sqlite'
    setup = '''
import sqlite3, sys
from datetime import datetime, timezone
from market_agent.adaptive_memory import MemoryRecord
from market_agent.persistent_research_memory import *
at = datetime(2026, 11, 1, tzinfo=timezone.utc)
connection = sqlite3.connect(sys.argv[1])
store = PersistentResearchMemory(connection)
'''
    writer = setup + '''
store.install_schema()
store.append(StoredMemory(tenant_id='synthetic-tenant', source_task_id='task-A',
    record=MemoryRecord(record_id='from-A', kind='ANSWER', identity='AI_DRAFT',
        symbol='APP', as_of=at, summary='prior cash conversion draft'), rights='SYNTHETIC', stored_at=at))
connection.commit()
connection.close()
'''
    reader = setup + '''
q = MemoryQuery(research_task_id='task-B', tenant_id='synthetic-tenant',
    question='APP cash', symbol='APP', cutoff=at, evaluation_time=at)
result = store.recall(q)
assert result.source_task_ids == ('task-A',)
assert result.records[0].record_id == 'from-A'
assert result.records[0].identity == 'AI_DRAFT'
assert not result.records[0].market_fact
connection.close()
print('FRESH_PROCESS_RECALL_VERIFIED')
'''
    environment = {k: v for k, v in os.environ.items()
                   if k in {'PATH', 'SYSTEMROOT', 'PYTHONPATH', 'MIA_ISOLATED_ROOT'}}
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    for script in (writer, reader):
        completed = subprocess.run([sys.executable, '-B', '-c', script, str(path)],
            capture_output=True, text=True, env=environment, timeout=15, check=True)
    assert 'FRESH_PROCESS_RECALL_VERIFIED' in completed.stdout


def test_invalidation_keeps_old_pit_but_current_reads_new_evidence(store):
    store.append(memory())
    store.append(memory('new', source_task_id='task-new', stored_at=at(3),
        record=memory('new').record.model_copy(update={'as_of': at(3), 'summary': 'APP cash risk worsened'})))
    store.transition('synthetic-tenant', 'old', kind='INVALIDATED', at=at(3), reason='new filing')
    assert ids(store.recall(query(cutoff=at(2)))) == ('old',)
    assert ids(store.recall(query())) == ('new',)
    assert store.connection.execute('SELECT count(*) FROM research_memory_v1').fetchone()[0] == 2


def test_withdrawal_revocation_and_expiry_are_excluded_before_selector(store):
    for name in ('withdrawn', 'revoked', 'private', 'expired', 'safe'):
        store.append(memory(name, rights='LOCAL_ONLY' if name == 'private' else 'SYNTHETIC',
                            expires_at=at(4) if name == 'expired' else None))
    store.transition('synthetic-tenant', 'withdrawn', kind='WITHDRAWN', at=at(2), reason='withdrawn')
    store.transition('synthetic-tenant', 'revoked', kind='RIGHTS_REVOKED', at=at(3), reason='rights ended')
    captured = []
    def selector(request):
        captured.append(request)
        return MemorySelectionDecision(selected_ids=('safe',), sufficient=True, reason_code='SUFFICIENT', trace_id='mock-1')
    result = store.recall(query(external=True, query_rights='SYNTHETIC'), selector=selector)
    assert ids(result) == ('safe',)
    assert [x.record.record_id for x in captured[0].candidates] == ['safe']
    assert captured[0].research_task_id == 'task-B'
    assert dict(result.excluded) == {'expired': 'EXPIRED', 'private': 'EGRESS_DENIED',
                                   'revoked': 'RIGHTS_REVOKED', 'withdrawn': 'WITHDRAWN'}
    # A historical query must not revive currently revoked egress.
    assert 'revoked' not in ids(store.recall(query(cutoff=at(2))))


def test_account_tenant_symbol_and_future_records_never_reach_selector(store):
    safe = memory()
    store.append(safe)
    store.append(memory('foreign-tenant', tenant_id='different'))
    store.append(memory('other-symbol', record=safe.record.model_copy(update={'record_id': 'other-symbol', 'symbol': 'BB'})))
    store.append(memory('future', stored_at=at(6)))
    store.append(memory('foreign-account', record=safe.record.model_copy(update={
        'record_id': 'foreign-account', 'scope_kind': 'ACCOUNT_BOUND', 'account_ids': ('synthetic-B',)})))
    result = store.recall(query(account_ids=('synthetic-A',)))
    assert ids(result) == ('old',)
    assert dict(result.excluded) == {'foreign-account': 'ACCOUNT_MISMATCH',
        'future': 'NOT_YET_AVAILABLE', 'other-symbol': 'SYMBOL_MISMATCH'}


def test_selector_cannot_escape_filtered_graph_or_expand_bounds(store):
    store.append(memory())
    for selected in [('foreign-private',), ('old', 'old')]:
        with pytest.raises(ValueError, match='out-of-scope'):
            store.recall(query(external=True, query_rights='SYNTHETIC'), selector=lambda _: MemorySelectionDecision(
                selected_ids=selected, sufficient=True, reason_code='TEST', trace_id='mock'))


def test_rights_change_during_selector_fails_closed(store):
    store.append(memory())
    def selector(_):
        store.transition('synthetic-tenant', 'old', kind='RIGHTS_REVOKED', at=at(4), reason='during request')
        return MemorySelectionDecision(selected_ids=('old',), sufficient=True, reason_code='TEST', trace_id='mock')
    with pytest.raises(ValueError, match='changed eligibility'):
        store.recall(query(external=True, query_rights='SYNTHETIC'), selector=selector)


def test_derived_cache_stale_corrupt_and_foreign_manifests_rebuild(store):
    store.append(memory())
    q = query()
    index = store.export_index(q)
    assert store.validate_index(q, index)
    assert not store.validate_index(query(tenant_id='other'), index)
    index['sha256'] = '0' * 64
    assert not store.validate_index(q, index)
    index = store.export_index(q)
    store.transition('synthetic-tenant', 'old', kind='INVALIDATED', at=at(3), reason='new evidence')
    assert not store.validate_index(q, index)
    assert not store.export_index(q)['payload']['records']


def test_atomic_caller_transaction_rolls_back_memory_and_event(store):
    with pytest.raises(RuntimeError):
        with store.connection:
            store.append(memory())
            store.transition('synthetic-tenant', 'old', kind='INVALIDATED', at=at(3), reason='test')
            raise RuntimeError('abort transaction')
    assert not store.recall(query()).records
    assert not store.connection.execute('SELECT * FROM research_memory_events_v1').fetchall()


def test_append_only_id_collision_and_direct_mutation_fail(store):
    store.append(memory())
    store.append(memory())
    with pytest.raises(ValueError, match='different bytes'):
        store.append(memory(source_task_id='changed-task'))
    for statement in ('UPDATE research_memory_v1 SET sha256="bad"', 'DELETE FROM research_memory_v1'):
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            store.connection.execute(statement)


def test_private_query_public_identity_and_invalid_lifecycle_fail_closed(store):
    with pytest.raises(ValueError, match='private question'):
        query(external=True)
    with pytest.raises(ValueError, match='sourced'):
        memory(rights='PUBLIC_SEC')
    with pytest.raises(ValueError, match='real account'):
        memory(record=memory().record.model_copy(update={'account_ids': ('real-A',)}))
    store.append(memory())
    with pytest.raises(ValueError, match='invalid memory lifecycle'):
        store.transition('synthetic-tenant', 'old', kind='INVALIDATED', at=at(1).replace(year=2025), reason='bad')


def test_public_sec_provenance_and_unverified_draft_identity_are_preserved(store):
    record = MemoryRecord(record_id='SEC-1', kind='EVIDENCE', identity='MARKET_FACT', verified=True,
                          symbol='APP', as_of=at(1), summary='issuer disclosed operating cash flow')
    store.append(memory('SEC-1', record=record, rights='PUBLIC_SEC', evidence_ids=('SEC-E1',),
                        source_uri='https://www.sec.gov/Archives/synthetic'))
    result = store.recall(query(external=True, query_rights='PUBLIC_SEC'))
    assert result.evidence_ids == ('SEC-E1',)
    assert result.records[0].market_fact
    with pytest.raises(ValueError, match='never be verified'):
        MemoryRecord(record_id='bad', kind='ANSWER', identity='AI_DRAFT', verified=True,
                     as_of=at(1), summary='not verified')


def test_recall_attachment_retains_task_symbol_account_and_cutoff_binding(store):
    store.append(memory())
    result = store.recall(query())
    assert result.context_role == 'MEMORY_CONTEXT_NOT_ORIGINAL_EVIDENCE'
    task = TaskMemory(thread_root_id='thread-B', current_task_id='task-B', question='APP cash',
        goal='INVESTIGATE', objective='EXPLAIN_CHANGE', symbol='APP', cutoff=at(4))
    assert result.to_task_memory(task).records[0].record_id == 'old'
    for updates in ({'current_task_id': 'foreign'}, {'symbol': 'BB'}, {'account_ids': ('synthetic-B',)},
                    {'cutoff': at(3)}):
        with pytest.raises(ValueError, match='different task'):
            result.to_task_memory(task.model_copy(update=updates))


def test_explicit_write_classification_preserves_identity_and_rejects_foreign_relations(store):
    store.append(memory())
    observation = memory('new', source_task_id='task-B')
    q = query(external=True, query_rights='SYNTHETIC')
    captured = []
    def classify(request):
        captured.append(request)
        return MemoryWriteDecision(should_store=True, annotation=MemoryAnnotation(category='SEMANTIC',
            related_ids=('old',), relation='CAUSAL_HYPOTHESIS', trace_id='OFFLINE-MOCK-WRITE'))
    assert store.append_classified(observation, q, classifier=classify)
    assert captured[0].research_task_id == 'task-B' and captured[0].purpose == 'jev.memory_build'
    assert all(x.rights != 'LOCAL_ONLY' for x in captured[0].candidates)
    raw = store.connection.execute('SELECT payload FROM research_memory_v1 WHERE record_id="new"').fetchone()[0]
    saved = StoredMemory.model_validate_json(raw)
    assert saved.record.identity == 'AI_DRAFT' and not saved.record.verified
    assert saved.annotation.relation == 'CAUSAL_HYPOTHESIS'
    with pytest.raises(ValueError, match='foreign relations'):
        store.append_classified(memory('bad', source_task_id='task-B'), q, classifier=lambda _: MemoryWriteDecision(
            should_store=True, annotation=MemoryAnnotation(category='SEMANTIC', related_ids=('foreign',), trace_id='mock')))
    with pytest.raises(ValueError, match='matching explicit public'):
        store.append_classified(memory('private', source_task_id='task-B', rights='LOCAL_ONLY'), q, classifier=classify)


def test_idempotent_append_supports_store_row_factory(store):
    store.connection.row_factory = sqlite3.Row
    store.append(memory())
    store.append(memory())
    assert ids(store.recall(query())) == ('old',)


def test_schema_install_preserves_frozen_legacy_rows_and_caller_transaction():
    connection = sqlite3.connect(':memory:')
    try:
        connection.execute('CREATE TABLE research_tasks (task_id TEXT PRIMARY KEY,payload BLOB)')
        frozen = b'{"version":"old-research-v1","status":"NEEDS_INPUT","raw":"\\u4e0d\\u53d8"}'
        connection.execute('INSERT INTO research_tasks VALUES (?,?)', ('legacy', frozen))
        connection.commit()
        connection.execute('BEGIN')
        store = PersistentResearchMemory(connection)
        store.install_schema()
        store.append(memory())
        assert connection.in_transaction
        assert connection.execute('SELECT payload FROM research_tasks').fetchone()[0] == frozen
        connection.rollback()
        assert connection.execute('SELECT payload FROM research_tasks').fetchone()[0] == frozen
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='research_memory_v1'").fetchall()
    finally:
        connection.close()
