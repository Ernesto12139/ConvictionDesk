"""Append-only cross-task memory; SQLite owns facts, indexes are disposable.

No credentials, network client, implicit migration or research execution lives
here. The Integrator owns the connection/transaction and injects any admitted
Jev selector. Memory remains provenance-labelled context, never new evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .adaptive_memory import MemoryRecord, TaskMemory
from .domain import FrozenModel, utc

MemoryRights = Literal['PUBLIC_SEC', 'ISSUER_PUBLIC', 'SYNTHETIC', 'LOCAL_ONLY']
UPSTREAM_COMMIT = '7ab0c73c6d8f4f611ad252c1e6ba8083f8df0e44'
UPSTREAM_SOURCE_SHA256 = '1d105f0fac4d68910c1036846818b8657c6571ecc8b526332febeb99d52905dc'


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class MemoryAnnotation(FrozenModel):
    """Jev classifications are annotations; they cannot promote a draft to fact."""
    category: Literal['EPISODIC', 'SEMANTIC', 'PROCEDURAL', 'PREFERENCE']
    related_ids: tuple[str, ...] = ()
    relation: Literal['NONE', 'SEMANTIC', 'TEMPORAL', 'ENTITY', 'CAUSAL_HYPOTHESIS'] = 'NONE'
    trace_id: str = Field(min_length=1, max_length=120)


class MemoryWriteDecision(FrozenModel):
    should_store: bool
    annotation: MemoryAnnotation


class StoredMemory(FrozenModel):
    version: Literal['persistent-memory-v1'] = 'persistent-memory-v1'
    tenant_id: str = Field(min_length=1, max_length=120)
    source_task_id: str = Field(min_length=1, max_length=80)
    record: MemoryRecord
    evidence_ids: tuple[str, ...] = ()
    source_uri: str | None = None
    rights: MemoryRights = 'LOCAL_ONLY'
    stored_at: datetime
    expires_at: datetime | None = None
    annotation: MemoryAnnotation | None = None

    _stored = field_validator('stored_at')(utc)
    _expires = field_validator('expires_at')(lambda x: None if x is None else utc(x))

    @model_validator(mode='after')
    def _public_provenance(self):
        if self.rights in {'PUBLIC_SEC', 'ISSUER_PUBLIC'}:
            if (not self.record.market_fact or not self.evidence_ids or not self.source_uri
                    or self.record.account_bound or self.record.account_ids):
                raise ValueError('public memory requires sourced account-agnostic verified evidence')
        if self.rights == 'SYNTHETIC' and self.record.account_ids:
            if not all(x.startswith('synthetic-') for x in self.record.account_ids):
                raise ValueError('synthetic memory cannot contain real account bindings')
        if self.expires_at is not None and self.expires_at <= self.stored_at:
            raise ValueError('memory expiry must follow storage')
        return self


class MemoryQuery(FrozenModel):
    research_task_id: str = Field(min_length=1, max_length=80)
    tenant_id: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=1200)
    symbol: str = Field(min_length=1, max_length=12)
    account_ids: tuple[str, ...] = ()
    cutoff: datetime
    evaluation_time: datetime
    query_rights: MemoryRights = 'LOCAL_ONLY'
    external: bool = False
    max_records: int = Field(default=12, ge=1, le=24)
    max_candidates: int = Field(default=24, ge=1, le=48)

    _cutoff = field_validator('cutoff')(utc)
    _time = field_validator('evaluation_time')(utc)
    _symbol = field_validator('symbol')(lambda x: x.removeprefix('$').upper())

    @model_validator(mode='after')
    def _bounds(self):
        if self.cutoff > self.evaluation_time:
            raise ValueError('memory cutoff cannot be in the future')
        if self.max_candidates < self.max_records:
            raise ValueError('candidate bound must cover record bound')
        if self.external and self.query_rights == 'LOCAL_ONLY':
            raise ValueError('private question cannot enter external memory selection')
        return self


class MemorySelectionRequest(FrozenModel):
    research_task_id: str
    question: str
    candidates: tuple[StoredMemory, ...]
    max_records: int
    purpose: Literal['jev.memory_query'] = 'jev.memory_query'


class MemoryWriteRequest(FrozenModel):
    research_task_id: str
    observation: StoredMemory
    candidates: tuple[StoredMemory, ...]
    purpose: Literal['jev.memory_build'] = 'jev.memory_build'


class MemorySelectionDecision(FrozenModel):
    selected_ids: tuple[str, ...]
    sufficient: bool
    reason_code: str = Field(min_length=1, max_length=120)
    trace_id: str = Field(min_length=1, max_length=120)


class MemoryRecall(FrozenModel):
    query: MemoryQuery
    context_role: Literal['MEMORY_CONTEXT_NOT_ORIGINAL_EVIDENCE'] = 'MEMORY_CONTEXT_NOT_ORIGINAL_EVIDENCE'
    records: tuple[MemoryRecord, ...]
    source_task_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...]
    mode: Literal['SQLITE_KEYWORD', 'INJECTED_JEV']
    corpus_sha256: str
    decision: MemorySelectionDecision | None = None

    def to_task_memory(self, task_memory: TaskMemory) -> TaskMemory:
        """Caller supplies the actual task identity; retain existing bounded type."""
        if (task_memory.current_task_id != self.query.research_task_id
                or task_memory.symbol != self.query.symbol or task_memory.cutoff != self.query.cutoff
                or set(task_memory.account_ids) != set(self.query.account_ids)):
            raise ValueError('recall cannot be attached to a different task/symbol/account/cutoff')
        result = task_memory
        for record in self.records:
            result = result.with_record(record)
        return result


class PersistentResearchMemory:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def install_schema(self) -> None:
        """Explicit caller-owned migration; no commits or changes to legacy tables."""
        statements = (
            '''CREATE TABLE IF NOT EXISTS research_memory_v1 (
                tenant_id TEXT NOT NULL, record_id TEXT NOT NULL,
                payload TEXT NOT NULL, sha256 TEXT NOT NULL,
                PRIMARY KEY (tenant_id,record_id))''',
            '''CREATE TABLE IF NOT EXISTS research_memory_events_v1 (
                tenant_id TEXT NOT NULL, record_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('INVALIDATED','WITHDRAWN','RIGHTS_REVOKED')),
                effective_at TEXT NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY (tenant_id,record_id,kind,effective_at),
                FOREIGN KEY (tenant_id,record_id) REFERENCES research_memory_v1(tenant_id,record_id))''',
        )
        for statement in statements:
            self.connection.execute(statement)
        for table in ('research_memory_v1', 'research_memory_events_v1'):
            for operation in ('UPDATE', 'DELETE'):
                self.connection.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_{operation.lower()}_blocked
                    BEFORE {operation} ON {table} BEGIN
                    SELECT RAISE(ABORT,'research memory history is append-only'); END''')

    def append(self, memory: StoredMemory) -> None:
        payload = memory.model_dump_json()
        existing = self.connection.execute(
            'SELECT payload,sha256 FROM research_memory_v1 WHERE tenant_id=? AND record_id=?',
            (memory.tenant_id, memory.record.record_id)).fetchone()
        if existing is not None:
            if tuple(existing) != (payload, _digest(payload)):
                raise ValueError('immutable memory id already contains different bytes')
            return
        self.connection.execute('INSERT INTO research_memory_v1 VALUES (?,?,?,?)',
                                (memory.tenant_id, memory.record.record_id, payload, _digest(payload)))

    def append_classified(self, memory: StoredMemory, query: MemoryQuery, *,
                          classifier: Callable[[MemoryWriteRequest], MemoryWriteDecision]) -> bool:
        """Explicit admitted write classification; never runs while rebuilding an index."""
        if (not query.external or memory.rights == 'LOCAL_ONLY' or memory.tenant_id != query.tenant_id
                or memory.source_task_id != query.research_task_id or memory.record.symbol != query.symbol
                or memory.stored_at > query.cutoff or memory.record.as_of > query.cutoff
                or not memory.record.pit_ok or memory.record.stale or memory.record.superseded_by
                or (memory.record.effective_cutoff or memory.record.as_of) > query.cutoff
                or memory.expires_at is not None and memory.expires_at <= query.evaluation_time
                or memory.record.account_ids and not set(memory.record.account_ids) <= set(query.account_ids)
                or memory.record.account_bound and not memory.record.account_ids and query.account_ids):
            raise ValueError('write classification requires matching explicit public scoped observation')
        eligible, _ = self._eligible(query)
        candidates = tuple(eligible[:query.max_candidates])
        decision = classifier(MemoryWriteRequest(research_task_id=query.research_task_id,
            observation=memory, candidates=candidates))
        if (len(decision.annotation.related_ids) != len(set(decision.annotation.related_ids))
                or not set(decision.annotation.related_ids) <= {x.record.record_id for x in candidates}):
            raise ValueError('write classifier proposed foreign relations')
        allowed_now = {x.record.record_id for x in self._eligible(query)[0]}
        if not set(decision.annotation.related_ids) <= allowed_now:
            raise ValueError('related memory changed eligibility during write classification')
        if not decision.should_store:
            return False
        self.append(memory.model_copy(update={'annotation': decision.annotation}))
        return True

    def transition(self, tenant_id: str, record_id: str, *,
                   kind: Literal['INVALIDATED', 'WITHDRAWN', 'RIGHTS_REVOKED'],
                   at: datetime, reason: str) -> None:
        at = utc(at)
        raw = self.connection.execute('SELECT payload,sha256 FROM research_memory_v1 '
                                      'WHERE tenant_id=? AND record_id=?', (tenant_id, record_id)).fetchone()
        if raw is None or _digest(raw[0]) != raw[1]:
            raise ValueError('unknown or corrupt canonical memory')
        memory = StoredMemory.model_validate_json(raw[0])
        if at < memory.stored_at or not reason.strip() or len(reason) > 200:
            raise ValueError('invalid memory lifecycle transition')
        self.connection.execute('INSERT INTO research_memory_events_v1 VALUES (?,?,?,?,?)',
                                (tenant_id, record_id, kind, at.isoformat(), reason))

    def _eligible(self, query: MemoryQuery) -> tuple[list[StoredMemory], list[tuple[str, str]]]:
        rows = self.connection.execute('SELECT payload,sha256 FROM research_memory_v1 '
                                       'WHERE tenant_id=? ORDER BY record_id', (query.tenant_id,)).fetchall()
        events = self.connection.execute('SELECT record_id,kind,effective_at FROM research_memory_events_v1 '
                                         'WHERE tenant_id=?', (query.tenant_id,)).fetchall()
        eligible, excluded = [], []
        for payload, digest in rows:
            if _digest(payload) != digest:
                raise ValueError('canonical memory integrity mismatch')
            item = StoredMemory.model_validate_json(payload)
            record, reason = item.record, None
            if record.symbol != query.symbol:
                reason = 'SYMBOL_MISMATCH'
            elif (record.account_ids and not set(record.account_ids) <= set(query.account_ids)
                  or record.account_bound and not record.account_ids and query.account_ids):
                reason = 'ACCOUNT_MISMATCH'
            elif (record.as_of > query.cutoff or (record.effective_cutoff or record.as_of) > query.cutoff
                  or item.stored_at > query.cutoff or not record.pit_ok):
                reason = 'NOT_YET_AVAILABLE'
            elif record.stale or record.superseded_by:
                reason = 'STALE_OR_SUPERSEDED'
            elif item.expires_at is not None and item.expires_at <= query.evaluation_time:
                reason = 'EXPIRED'
            elif query.external and item.rights == 'LOCAL_ONLY':
                reason = 'EGRESS_DENIED'
            for record_id, kind, effective in events:
                if record_id != record.record_id:
                    continue
                # Rights withdrawal is always enforced now, even for a historical question.
                horizon = query.evaluation_time if kind == 'RIGHTS_REVOKED' else query.cutoff
                if datetime.fromisoformat(effective) <= horizon:
                    reason = kind
            if reason:
                excluded.append((record.record_id, reason))
            else:
                eligible.append(item)
        return eligible, excluded

    def recall(self, query: MemoryQuery, *,
               selector: Callable[[MemorySelectionRequest], MemorySelectionDecision] | None = None) -> MemoryRecall:
        eligible, excluded = self._eligible(query)
        corpus_hash = _digest('\n'.join(item.model_dump_json() for item in eligible))
        terms = set(re.findall(r'\w+', query.question.casefold()))
        ranked = sorted(eligible, key=lambda item: (
            -len(terms & set(re.findall(r'\w+', item.record.summary.casefold()))),
            -item.record.as_of.timestamp(), item.record.record_id))[:query.max_candidates]
        decision = None
        if selector is not None:
            if not query.external:
                raise ValueError('Jev selector requires explicit external scope')
            if ranked:
                decision = selector(MemorySelectionRequest(research_task_id=query.research_task_id,
                    question=query.question, candidates=tuple(ranked), max_records=query.max_records))
                ids = decision.selected_ids
                if (len(ids) != len(set(ids)) or len(ids) > query.max_records
                        or not set(ids) <= {x.record.record_id for x in ranked}):
                    raise ValueError('selector returned out-of-scope or unbounded memory')
                by_id = {x.record.record_id: x for x in ranked}
                ranked = [by_id[x] for x in ids]
                # Recheck scope/lifecycle after the admitted call, before answer egress.
                allowed_now = {x.record.record_id for x in self._eligible(query)[0]}
                if any(x.record.record_id not in allowed_now for x in ranked):
                    raise ValueError('memory changed eligibility during selection')
        selected = ranked[:query.max_records]
        return MemoryRecall(query=query, records=tuple(x.record for x in selected),
            source_task_ids=tuple(dict.fromkeys(x.source_task_id for x in selected)),
            evidence_ids=tuple(dict.fromkeys(e for x in selected for e in x.evidence_ids)),
            excluded=tuple(excluded), mode='INJECTED_JEV' if selector is not None else 'SQLITE_KEYWORD',
            corpus_sha256=corpus_hash, decision=decision)

    def export_index(self, query: MemoryQuery) -> dict:
        """Disposable filtered index manifest; no private global cache can escape."""
        records, _ = self._eligible(query)
        payload = {'version': 'research-memory-index-v1', 'upstream_commit': UPSTREAM_COMMIT,
                   'query_scope_sha256': _digest(query.model_dump_json()),
                   'records': [x.model_dump(mode='json') for x in records]}
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        return {'payload': payload, 'sha256': _digest(encoded)}

    def validate_index(self, query: MemoryQuery, index: dict) -> bool:
        """Corrupt/stale/foreign derived cache is ignored; rebuild from SQLite."""
        return index == self.export_index(query)
