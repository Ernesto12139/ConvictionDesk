"""Explicit SQLite repositories for immutable snapshots and observable runs."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from .domain import AgentAnswer, PortfolioSnapshot, RunRecord, RunTrace, utc
from .test_isolation import isolated_database


class Store:
    def __init__(self, path: str | Path):
        self.path = isolated_database(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    snapshot_id TEXT PRIMARY KEY, as_of TEXT NOT NULL,
                    content_hash TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS query_runs (
                    run_id TEXT PRIMARY KEY, query_text TEXT NOT NULL,
                    status TEXT NOT NULL, trace_json TEXT, answer_json TEXT);
                CREATE TABLE IF NOT EXISTS tool_calls (
                    run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY (run_id, sequence),
                    FOREIGN KEY (run_id) REFERENCES query_runs(run_id));
                CREATE TABLE IF NOT EXISTS evidence (
                    run_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY (run_id, evidence_id),
                    FOREIGN KEY (run_id) REFERENCES query_runs(run_id));
                CREATE TABLE IF NOT EXISTS provider_cache (
                    provider TEXT NOT NULL, symbol TEXT NOT NULL,
                    requested_as_of TEXT NOT NULL, observation_as_of TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (provider, symbol, requested_as_of));
                CREATE TABLE IF NOT EXISTS sec_documents (
                    document_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, cik TEXT NOT NULL,
                    accession_number TEXT NOT NULL, form TEXT NOT NULL, filed_at TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL, source_uri TEXT NOT NULL,
                    content_hash TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sec_chunks (
                    chunk_id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL, section TEXT NOT NULL,
                    text TEXT NOT NULL, content_hash TEXT NOT NULL, payload TEXT NOT NULL,
                    FOREIGN KEY (document_id) REFERENCES sec_documents(document_id));
                CREATE VIRTUAL TABLE IF NOT EXISTS sec_chunks_fts USING fts5(
                    chunk_id UNINDEXED, symbol UNINDEXED, section, text);
                CREATE TABLE IF NOT EXISTS thesis_versions (
                    thesis_version_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                    as_of TEXT NOT NULL, status TEXT NOT NULL,
                    evidence_id TEXT NOT NULL, payload TEXT NOT NULL,
                    evidence_payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS decision_records (
                    decision_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                    analysis_cutoff TEXT NOT NULL, recorded_at TEXT NOT NULL,
                    record_kind TEXT NOT NULL, record_hash TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES query_runs(run_id));
                CREATE TABLE IF NOT EXISTS decision_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cockpit_results (
                    cockpit_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    record_hash TEXT NOT NULL, payload TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES query_runs(run_id));
                CREATE TRIGGER IF NOT EXISTS cockpit_results_no_update
                    BEFORE UPDATE ON cockpit_results BEGIN
                    SELECT RAISE(ABORT, 'cockpit results are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS cockpit_results_no_delete
                    BEFORE DELETE ON cockpit_results BEGIN
                    SELECT RAISE(ABORT, 'cockpit results are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS cockpit_results_no_replace
                    BEFORE INSERT ON cockpit_results
                    WHEN EXISTS (SELECT 1 FROM cockpit_results WHERE cockpit_id = NEW.cockpit_id)
                    BEGIN SELECT RAISE(ABORT, 'cockpit results are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS decision_checkpoints_no_update
                    BEFORE UPDATE ON decision_checkpoints BEGIN
                    SELECT RAISE(ABORT, 'decision checkpoints are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS decision_checkpoints_no_delete
                    BEFORE DELETE ON decision_checkpoints BEGIN
                    SELECT RAISE(ABORT, 'decision checkpoints are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS decision_checkpoints_no_replace
                    BEFORE INSERT ON decision_checkpoints
                    WHEN EXISTS (SELECT 1 FROM decision_checkpoints WHERE checkpoint_id = NEW.checkpoint_id)
                    BEGIN SELECT RAISE(ABORT, 'decision checkpoints are immutable'); END;
                CREATE INDEX IF NOT EXISTS decision_records_previous
                    ON decision_records (symbol, record_kind, analysis_cutoff DESC,
                                         recorded_at DESC, decision_id DESC);
                CREATE TRIGGER IF NOT EXISTS decision_records_no_update
                    BEFORE UPDATE ON decision_records BEGIN
                    SELECT RAISE(ABORT, 'decision records are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS decision_records_no_delete
                    BEFORE DELETE ON decision_records BEGIN
                    SELECT RAISE(ABORT, 'decision records are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS decision_records_no_replace
                    BEFORE INSERT ON decision_records
                    WHEN EXISTS (SELECT 1 FROM decision_records WHERE decision_id = NEW.decision_id)
                    BEGIN SELECT RAISE(ABORT, 'decision records are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS portfolio_snapshots_no_update
                    BEFORE UPDATE ON portfolio_snapshots BEGIN
                    SELECT RAISE(ABORT, 'portfolio snapshots are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS portfolio_snapshots_no_delete
                    BEFORE DELETE ON portfolio_snapshots BEGIN
                    SELECT RAISE(ABORT, 'portfolio snapshots are immutable'); END;
            """)

    def add_cockpit_result(self, result) -> None:
        from .cockpit import CockpitResult
        result = CockpitResult.model_validate(result.model_dump())
        run = self.get_run(result.run_id)
        if not run or run.status != "success":
            raise ValueError("cockpit requires a completed persisted run")
        brief = result.brief
        statements = (brief.one_line_decision, *brief.top_reasons, brief.main_counterargument,
                      *brief.what_changes_my_mind, brief.next_best_check)
        statements = (*statements, *getattr(brief, "conditional_branches", ()))
        ids = {i.evidence_id for i in self.get_run_evidence(result.run_id)}
        if set(brief.decision_evidence_ids) - ids or any(set(s.evidence_ids) - ids for s in statements if s):
            raise ValueError("cockpit references outside frozen run evidence")
        if result.version == 2:
            from .fast_decision import validate_fast
            packet = self.get_decision_checkpoint(result.checkpoint_id)
            if (not packet or packet["facts"]["decision_framing"] != result.framing.model_dump(mode="json") or
                {i["evidence_id"] for i in packet["selected"]} != ids or
                not any(c.name == "decision.fast" and c.status == "success" and
                    c.output_refs == [result.checkpoint_id] for c in run.trace.tool_calls)):
                raise ValueError("Fast checkpoint/run lineage mismatch")
            validate_fast(brief, packet)
        if result.decision_id:
            decision = self.get_decision(result.decision_id)
            if (not decision or decision.run_id != result.run_id or
                decision.symbol != result.framing.symbol or
                decision.decision_price != result.framing.decision_price or
                decision.decision_price_source != result.framing.price_source or
                decision.portfolio_snapshot_id != result.framing.snapshot_id or
                decision.investment_context.intent != result.framing.intent):
                raise ValueError("cockpit and frozen decision disagree")
        with self._connect() as db:
            db.execute("INSERT INTO cockpit_results VALUES (?, ?, ?, ?)",
                (result.cockpit_id, result.run_id, result.record_hash, result.model_dump_json()))

    def get_cockpit_result(self, cockpit_id):
        from .cockpit import CockpitResult
        from .tdb import TDBError
        with self._connect() as db:
            row = db.execute("SELECT payload, record_hash FROM cockpit_results WHERE cockpit_id=?", (cockpit_id,)).fetchone()
        if not row:
            return None
        try:
            result = CockpitResult.model_validate_json(row[0])
            if result.record_hash != row[1]:
                raise ValueError("cockpit stored hash mismatch")
            return result
        except ValueError:
            raise TDBError("COCKPIT_HISTORY_INVALID", 409) from None

    def add_thesis(self, thesis, evidence) -> None:
        values = self._thesis_values(thesis, evidence)
        with self._connect() as db:
            db.execute("INSERT INTO thesis_versions VALUES (?, ?, ?, ?, ?, ?, ?)", values)

    @staticmethod
    def _thesis_values(thesis, evidence) -> tuple:
        from .thesis import ThesisProfile
        from .domain import EvidenceItem

        thesis = ThesisProfile.model_validate(thesis.model_dump())
        evidence = EvidenceItem.model_validate(evidence.model_dump())
        if evidence.evidence_type != "thesis" or evidence.source_record_id != thesis.thesis_version_id:
            raise ValueError("thesis evidence does not identify the thesis version")
        return (
            thesis.thesis_version_id,
            thesis.symbol,
            thesis.as_of.isoformat(),
            thesis.status.value,
            evidence.evidence_id,
            thesis.model_dump_json(),
            evidence.model_dump_json(),
        )

    def get_thesis(self, symbol: str, as_of: datetime | None = None):
        from .thesis import ThesisProfile

        with self._connect() as db:
            if as_of is None:
                row = db.execute(
                    "SELECT payload FROM thesis_versions WHERE symbol=? ORDER BY as_of DESC, rowid DESC LIMIT 1",
                    (symbol,),
                ).fetchone()
            else:
                row = db.execute(
                    """SELECT payload FROM thesis_versions WHERE symbol=? AND as_of <= ?
                    ORDER BY as_of DESC, rowid DESC LIMIT 1""",
                    (symbol, utc(as_of).isoformat()),
                ).fetchone()
        return ThesisProfile.model_validate_json(row[0]) if row else None

    def get_thesis_evidence(self, thesis_version_id: str):
        from .domain import EvidenceItem

        with self._connect() as db:
            row = db.execute(
                "SELECT evidence_payload FROM thesis_versions WHERE thesis_version_id=?",
                (thesis_version_id,),
            ).fetchone()
        return EvidenceItem.model_validate_json(row[0]) if row else None

    def get_thesis_source_evidence(self, evidence_id: str, thesis_created_at: datetime):
        from .domain import EvidenceItem

        # A thesis must retain the source version that existed when it was created.
        # A later public retrieval can be PIT-eligible in general (D-015), but cannot
        # retroactively become an input to this persisted thesis (D-034).
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM evidence WHERE evidence_id=? ORDER BY rowid DESC",
                (evidence_id,),
            ).fetchall()
        eligible = []
        for row in rows:
            item = EvidenceItem.model_validate_json(row[0])
            if (item.retrieved_at <= thesis_created_at
                    and (item.as_of is None or item.as_of <= thesis_created_at)
                    and (item.published_at is None or item.published_at <= thesis_created_at)):
                eligible.append(item)
        return max(eligible, key=lambda item: (item.retrieved_at, item.content_hash),
                   default=None)

    def add_decision(self, record, trace: RunTrace, evidence, thesis_record=None) -> None:
        from .tdb import DecisionRecord
        from .domain import EvidenceItem

        record = DecisionRecord.model_validate(record.model_dump())
        trace = RunTrace.model_validate(trace.model_dump())
        items = [EvidenceItem.model_validate(item.model_dump()) for item in evidence]
        if record.run_id != trace.run_id or set(trace.evidence_ids) != {
            item.evidence_id for item in items
        }:
            raise ValueError("decision trace and evidence do not match")
        if trace.final_claim_links != record.manifest.claim_links:
            raise ValueError("decision trace and manifest claim links do not match")
        expected_refs = {item.evidence_id: item.model_dump(mode="json") for item in record.manifest.evidence}
        if len(expected_refs) != len(items):
            raise ValueError("decision manifest and evidence do not match")
        from .tdb import EvidenceReference
        if expected_refs != {
            item.evidence_id: EvidenceReference.from_evidence(item).model_dump(mode="json")
            for item in items
        }:
            raise ValueError("decision manifest and evidence do not match")
        with self._connect() as db:
            if thesis_record is not None:
                thesis, thesis_evidence = thesis_record
                if record.thesis_version_id != thesis.thesis_version_id or (
                    thesis_evidence.evidence_id not in {item.evidence_id for item in items}
                ):
                    raise ValueError("decision and generated thesis do not match")
                db.execute("INSERT INTO thesis_versions VALUES (?, ?, ?, ?, ?, ?, ?)",
                           self._thesis_values(thesis, thesis_evidence))
            db.execute("INSERT INTO query_runs (run_id, query_text, status, trace_json) VALUES (?, ?, 'success', ?)",
                       (trace.run_id, trace.query, trace.model_dump_json()))
            for call in trace.tool_calls:
                db.execute("INSERT INTO tool_calls VALUES (?, ?, ?)",
                           (trace.run_id, call.sequence, call.model_dump_json()))
            for item in items:
                db.execute("INSERT INTO evidence VALUES (?, ?, ?)",
                           (trace.run_id, item.evidence_id, item.model_dump_json()))
            db.execute(
                "INSERT INTO decision_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (record.decision_id, record.symbol, record.analysis_cutoff.isoformat(),
                 record.recorded_at.isoformat(), record.record_kind, record.record_hash, record.run_id,
                 record.model_dump_json()),
            )

    def record_failed_decision_run(self, trace: RunTrace, evidence) -> None:
        from .domain import EvidenceItem

        trace = RunTrace.model_validate(trace.model_dump())
        items = [EvidenceItem.model_validate(item.model_dump()) for item in evidence]
        if set(trace.evidence_ids) != {item.evidence_id for item in items}:
            raise ValueError("failed decision trace and evidence do not match")
        with self._connect() as db:
            db.execute(
                "INSERT INTO query_runs (run_id, query_text, status, trace_json) VALUES (?, ?, 'error', ?)",
                (trace.run_id, trace.query, trace.model_dump_json()),
            )
            for call in trace.tool_calls:
                db.execute("INSERT INTO tool_calls VALUES (?, ?, ?)",
                           (trace.run_id, call.sequence, call.model_dump_json()))
            for item in items:
                db.execute("INSERT INTO evidence VALUES (?, ?, ?)",
                           (trace.run_id, item.evidence_id, item.model_dump_json()))

    def get_decision(self, decision_id: str):
        with self._connect() as db:
            row = db.execute("SELECT payload FROM decision_records WHERE decision_id=?", (decision_id,)).fetchone()
        return self._read_decision(row[0]) if row else None

    @staticmethod
    def _read_decision(payload: str):
        from .tdb import DecisionRecord, TDBError

        try:
            return DecisionRecord.model_validate_json(payload)
        except ValidationError:
            # Keep invalid history closed and untouched; never leak input values.
            raise TDBError("DECISION_HISTORY_INVALID", 409) from None

    def get_previous_qualified_decision(self, symbol: str, before: datetime):
        cutoff = utc(before).isoformat()
        # Review time wins; recorded time and ID make revisions/ties deterministic.
        with self._connect() as db:
            row = db.execute(
                """SELECT payload FROM decision_records
                WHERE symbol=? AND record_kind='QUALIFIED_REVIEW'
                  AND recorded_at <= ? AND analysis_cutoff <= ?
                ORDER BY analysis_cutoff DESC, recorded_at DESC, decision_id DESC LIMIT 1""",
                (symbol, cutoff, cutoff),
            ).fetchone()
        return self._read_decision(row[0]) if row else None

    def freeze_decision_checkpoint(self, checkpoint_id, payload):
        from .tdb import content_hash
        encoded = json.dumps(payload, ensure_ascii=False)
        with self._connect() as db:
            db.execute("INSERT INTO decision_checkpoints VALUES (?, ?, ?)",
                       (checkpoint_id, content_hash(payload), encoded))

    def get_decision_checkpoint(self, checkpoint_id):
        from .tdb import content_hash
        with self._connect() as db:
            row = db.execute("SELECT payload_hash, payload FROM decision_checkpoints WHERE checkpoint_id=?",
                             (checkpoint_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row[1])
        if content_hash(payload) != row[0]:
            raise ValueError("decision checkpoint hash mismatch")
        return payload

    def get_run_evidence(self, run_id: str):
        from .domain import EvidenceItem

        with self._connect() as db:
            rows = db.execute("SELECT payload FROM evidence WHERE run_id=?", (run_id,)).fetchall()
        return [EvidenceItem.model_validate_json(row[0]) for row in rows]

    def get_persisted_event_evidence(self, symbol: str, cutoff: datetime, limit: int = 20):
        """Use only vintages actually stored by the cutoff for historical review."""
        from .domain import EvidenceItem

        cutoff = utc(cutoff)
        with self._connect() as db:
            rows = db.execute("""SELECT e.payload FROM evidence AS e
                JOIN query_runs AS q ON q.run_id = e.run_id
                WHERE q.status = 'success' ORDER BY e.rowid DESC""").fetchall()
        selected = {}
        for row in rows:
            item = EvidenceItem.model_validate_json(row[0])
            if (item.symbol == symbol and item.evidence_type in {"news", "sec_filing"}
                    and item.retrieved_at <= cutoff
                    and item.published_at is not None and item.published_at <= cutoff):
                selected.setdefault(item.evidence_id, item)
        return sorted(selected.values(), key=lambda item: (item.published_at, item.evidence_id),
                      reverse=True)[:limit]

    def get_previous_semantic_decision(self, symbol: str, before: datetime):
        cutoff = utc(before).isoformat()
        with self._connect() as db:
            rows = db.execute(
                """SELECT payload FROM decision_records
                WHERE symbol=? AND record_kind='QUALIFIED_REVIEW'
                  AND recorded_at <= ? AND analysis_cutoff <= ?
                ORDER BY analysis_cutoff DESC, recorded_at DESC, decision_id DESC""",
                (symbol, cutoff, cutoff),
            ).fetchall()
        for row in rows:
            record = self._read_decision(row[0])
            if record.version >= 2 and record.semantic is not None:
                return record
        return None

    def index_sec_document(self, document, chunks) -> None:
        """Persist immutable filing/chunk metadata and its FTS5 search rows."""
        with self._connect() as db:
            inserted = db.execute(
                """INSERT OR IGNORE INTO sec_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    document.document_id,
                    document.symbol,
                    document.cik,
                    document.accession_number,
                    document.form,
                    document.filed_at.isoformat(),
                    document.retrieved_at.isoformat(),
                    document.source_uri,
                    document.content_hash,
                    document.model_dump_json(),
                ),
            ).rowcount
            if not inserted:
                return
            for chunk in chunks:
                db.execute(
                    "INSERT INTO sec_chunks VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk.chunk_id,
                        chunk.document_id,
                        chunk.sequence,
                        chunk.section,
                        chunk.text,
                        chunk.content_hash,
                        chunk.model_dump_json(),
                    ),
                )
                db.execute(
                    "INSERT INTO sec_chunks_fts (chunk_id, symbol, section, text) VALUES (?, ?, ?, ?)",
                    (chunk.chunk_id, document.symbol, chunk.section, chunk.text),
                )

    def search_sec_chunks(self, symbol: str, query: str, as_of: datetime,
                          forms: list[str], limit: int):
        """Return ranked eligible SEC chunks; later filings are excluded in SQL."""
        import re
        from .sec import SecSearchChunk

        tokens = re.findall(r"[A-Za-z0-9]+", query.lower())
        if not tokens or not forms or limit < 1:
            return []
        match = " OR ".join(f'"{token}"' for token in tokens[:20])
        placeholders = ",".join("?" for _ in forms)
        sql = f"""
            SELECT c.payload, d.payload
            FROM sec_chunks_fts AS f
            JOIN sec_chunks AS c ON c.chunk_id = f.chunk_id
            JOIN sec_documents AS d ON d.document_id = c.document_id
            WHERE sec_chunks_fts MATCH ? AND f.symbol = ?
              AND d.filed_at <= ? AND d.form IN ({placeholders})
            ORDER BY bm25(sec_chunks_fts), d.filed_at DESC, c.sequence ASC
            LIMIT ?
        """
        params = [match, symbol, utc(as_of).isoformat(), *forms, limit]
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [
            SecSearchChunk.from_payloads(json.loads(chunk), json.loads(document))
            for chunk, document in rows
        ]

    def eligible_sec_chunks(self, symbol: str, as_of: datetime, forms: list[str], limit: int):
        """Bounded vector candidates with the SAME public SEC eligibility as FTS5.

        SEC acceptance, not the date a public filing was downloaded, defines PIT.
        The extra row lets callers reject partial vector coverage explicitly.
        """
        from .sec import SecSearchChunk
        cutoff = utc(as_of)
        if not forms or limit < 1:
            return []
        placeholders = ','.join('?' for _ in forms)
        with self._connect() as db:
            rows = db.execute(f'''SELECT c.payload,d.payload FROM sec_chunks AS c
                JOIN sec_documents AS d ON d.document_id=c.document_id
                WHERE d.symbol=? AND d.filed_at<=? AND d.form IN ({placeholders})
                ORDER BY d.filed_at DESC,c.sequence ASC,c.chunk_id ASC LIMIT ?''',
                (symbol,cutoff.isoformat(),*forms,limit)).fetchall()
        return [SecSearchChunk.from_payloads(json.loads(c),json.loads(d)) for c,d in rows]

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def add_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        # model_copy can bypass Pydantic validation, so revalidate at the persistence boundary.
        snapshot = PortfolioSnapshot.model_validate(snapshot.model_dump())
        with self._connect() as db:
            db.execute("INSERT INTO portfolio_snapshots VALUES (?, ?, ?, ?)",
                       (snapshot.snapshot_id, snapshot.as_of.isoformat(), snapshot.content_hash,
                        snapshot.model_dump_json()))

    def get_provider_cache(self, provider: str, symbol: str, requested_as_of: datetime):
        from .provider import MarketDataResult

        with self._connect() as db:
            row = db.execute(
                "SELECT retrieved_at, payload FROM provider_cache WHERE provider=? AND symbol=? AND requested_as_of=?",
                (provider, symbol, utc(requested_as_of).isoformat()),
            ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row[0]), MarketDataResult.model_validate_json(row[1])

    def add_snapshot_if_current(self, snapshot: PortfolioSnapshot, expected_id: str | None) -> None:
        snapshot = PortfolioSnapshot.model_validate(snapshot.model_dump())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT snapshot_id FROM portfolio_snapshots ORDER BY as_of DESC, rowid DESC LIMIT 1"
            ).fetchone()
            if (row[0] if row else None) != expected_id:
                raise ValueError("PORTFOLIO_CHANGED")
            db.execute("INSERT INTO portfolio_snapshots VALUES (?, ?, ?, ?)",
                       (snapshot.snapshot_id, snapshot.as_of.isoformat(), snapshot.content_hash,
                        snapshot.model_dump_json()))

    def put_provider_cache(self, provider: str, symbol: str, requested_as_of: datetime,
                           retrieved_at: datetime, result) -> None:
        if len(result.observations) != 1 or result.observations[0].symbol != symbol:
            raise ValueError("cache requires one matching observation")
        with self._connect() as db:
            db.execute(
                """INSERT INTO provider_cache VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, symbol, requested_as_of) DO UPDATE SET
                observation_as_of=excluded.observation_as_of,
                retrieved_at=excluded.retrieved_at, payload=excluded.payload""",
                (provider, symbol, utc(requested_as_of).isoformat(),
                 result.observations[0].as_of.isoformat(), utc(retrieved_at).isoformat(),
                 result.model_dump_json()),
            )

    def get_snapshot(self, as_of: datetime | None = None) -> PortfolioSnapshot | None:
        with self._connect() as db:
            if as_of is None:
                row = db.execute("SELECT payload FROM portfolio_snapshots ORDER BY as_of DESC, rowid DESC LIMIT 1").fetchone()
            else:
                row = db.execute("SELECT payload FROM portfolio_snapshots WHERE as_of <= ? ORDER BY as_of DESC, rowid DESC LIMIT 1",
                                 (as_of.isoformat(),)).fetchone()
        return PortfolioSnapshot.model_validate_json(row[0]) if row else None

    def get_snapshot_known_at(self, cutoff: datetime) -> PortfolioSnapshot | None:
        """Select the latest snapshot that both represents and predates the cutoff."""
        cutoff = utc(cutoff)
        with self._connect() as db:
            rows = db.execute(
                """SELECT payload FROM portfolio_snapshots WHERE as_of <= ?
                ORDER BY as_of DESC, rowid DESC""",
                (cutoff.isoformat(),),
            ).fetchall()
        for row in rows:
            snapshot = PortfolioSnapshot.model_validate_json(row[0])
            if snapshot.source.imported_at <= cutoff:
                return snapshot
        return None

    def start_run(self, run_id: str, query: str) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO query_runs (run_id, query_text, status) VALUES (?, ?, 'running')", (run_id, query))

    def finish_run(self, trace: RunTrace, answer: AgentAnswer, thesis_record=None) -> None:
        thesis_values = (
            self._thesis_values(*thesis_record) if thesis_record is not None else None
        )
        with self._connect() as db:
            db.execute("UPDATE query_runs SET status='success', trace_json=?, answer_json=? WHERE run_id=?",
                       (trace.model_dump_json(), answer.model_dump_json(), trace.run_id))
            for call in trace.tool_calls:
                db.execute("INSERT INTO tool_calls VALUES (?, ?, ?)",
                           (trace.run_id, call.sequence, call.model_dump_json()))
            for item in answer.evidence:
                db.execute("INSERT INTO evidence VALUES (?, ?, ?)",
                           (trace.run_id, item.evidence_id, item.model_dump_json()))
            if thesis_values is not None:
                db.execute(
                    "INSERT INTO thesis_versions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    thesis_values,
                )

    def fail_run(self, run_id: str, trace: RunTrace) -> None:
        with self._connect() as db:
            db.execute("UPDATE query_runs SET status='error', trace_json=? WHERE run_id=?",
                       (trace.model_dump_json(), run_id))
            for call in trace.tool_calls:
                db.execute("INSERT INTO tool_calls VALUES (?, ?, ?)",
                           (run_id, call.sequence, call.model_dump_json()))

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT status, trace_json, answer_json FROM query_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        return RunRecord(status=row[0], trace=json.loads(row[1]) if row[1] else None,
                         answer=json.loads(row[2]) if row[2] else None)
