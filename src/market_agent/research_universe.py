"""Small research lifecycle; position ownership always comes from the portfolio."""
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator

from .domain import FrozenModel, utc
from .persistence import Store
from .provider import SYMBOL
from .tdb import TDBError, content_hash

UniverseState = Literal["HOLDING", "WATCHING", "CANDIDATE", "ARCHIVED"]
ManagedState = Literal["WATCHING", "CANDIDATE", "ARCHIVED"]


class UniverseChange(FrozenModel):
    symbol: str
    state: ManagedState
    reason: str = Field(default="Owner research", min_length=1, max_length=300)
    owner_note: str = Field(default="", max_length=500)

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value):
        value = value.strip().upper()
        if not SYMBOL.fullmatch(value):
            raise ValueError("invalid US equity symbol")
        return value


class UniverseEntry(FrozenModel):
    symbol: str
    state: UniverseState
    created_at: datetime
    updated_at: datetime
    reason: str
    owner_note: str = ""
    last_attention_cutoff: datetime | None = None
    snapshot_id: str | None = None
    # Cost exposure, not market NAV weight. Missing portfolio remains unknown.
    cost_exposure_pct: Decimal | None = None
    exposure_basis: Literal["COST_BASIS_PLUS_CASH", "UNKNOWN"] = "UNKNOWN"
    thesis_version_id: str | None = None

    _times = field_validator("created_at", "updated_at")(utc)
    _cutoff = field_validator("last_attention_cutoff")(lambda v: utc(v) if v else None)


class ResearchStore:
    """Additive tables only; immutable prior portfolio/decision bytes are untouched."""
    def __init__(self, store: Store):
        self.store = store
        with store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS research_universe (
                    symbol TEXT PRIMARY KEY, state TEXT NOT NULL
                    CHECK(state IN ('WATCHING','CANDIDATE','ARCHIVED')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    reason TEXT NOT NULL, owner_note TEXT NOT NULL,
                    last_attention_cutoff TEXT);
                CREATE TABLE IF NOT EXISTS research_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attention_events (
                    event_key TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                    payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    screen TEXT, screen_hash TEXT, scan_id TEXT);
                CREATE TABLE IF NOT EXISTS attention_aliases (
                    symbol TEXT NOT NULL, alias TEXT NOT NULL, event_key TEXT NOT NULL,
                    PRIMARY KEY(symbol,alias));
                CREATE TABLE IF NOT EXISTS attention_items (
                    item_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                    payload TEXT NOT NULL, payload_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attention_reviewed (
                    item_id TEXT PRIMARY KEY, reviewed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attention_event_groups (
                    group_id TEXT PRIMARY KEY, payload TEXT NOT NULL, payload_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attention_deep_inputs (
                    deep_id TEXT PRIMARY KEY, payload TEXT NOT NULL, payload_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attention_scans (
                    scan_id TEXT PRIMARY KEY, status TEXT NOT NULL, started_at TEXT NOT NULL,
                    payload TEXT, payload_hash TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS attention_one_scan
                    ON attention_scans(status) WHERE status='RUNNING';
                CREATE TABLE IF NOT EXISTS attention_deep (
                    deep_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, status TEXT NOT NULL,
                    started_at TEXT NOT NULL, payload TEXT, payload_hash TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS attention_one_deep
                    ON attention_deep(item_id) WHERE status='RUNNING';
                CREATE TRIGGER IF NOT EXISTS attention_event_frozen
                    BEFORE UPDATE OF payload,payload_hash,symbol,event_key ON attention_events
                    BEGIN SELECT RAISE(ABORT,'attention evidence is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS attention_item_frozen
                    BEFORE UPDATE ON attention_items
                    BEGIN SELECT RAISE(ABORT,'attention items are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS attention_screen_frozen
                    BEFORE UPDATE OF screen,screen_hash ON attention_events WHEN OLD.screen IS NOT NULL
                    BEGIN SELECT RAISE(ABORT,'attention screening is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS attention_scan_frozen
                    BEFORE UPDATE ON attention_scans WHEN OLD.payload IS NOT NULL
                    BEGIN SELECT RAISE(ABORT,'attention scan report is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS attention_deep_frozen
                    BEFORE UPDATE ON attention_deep WHEN OLD.payload IS NOT NULL
                    BEGIN SELECT RAISE(ABORT,'attention deep result is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS attention_group_frozen
                    BEFORE UPDATE ON attention_event_groups
                    BEGIN SELECT RAISE(ABORT,'attention event grouping is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS attention_deep_input_frozen
                    BEFORE UPDATE ON attention_deep_inputs
                    BEGIN SELECT RAISE(ABORT,'attention deep input is immutable'); END;
            """)

    def initialize(self, now):
        """One-time legacy watchlist import; never re-import an archived symbol."""
        snapshot = self.store.get_snapshot_known_at(now)
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM research_meta WHERE name='watchlist_import_v1'").fetchone() and snapshot:
                for symbol in snapshot.watchlist:
                    if SYMBOL.fullmatch(symbol):
                        db.execute("INSERT OR IGNORE INTO research_universe VALUES (?,?,?,?,?,?,NULL)",
                            (symbol, 'WATCHING', now.isoformat(), now.isoformat(), 'Legacy snapshot watchlist', ''))
                db.execute("INSERT INTO research_meta VALUES ('watchlist_import_v1',?)", (snapshot.snapshot_id,))

    def entries(self, now):
        now = utc(now)
        self.initialize(now)
        snapshot = self.store.get_snapshot_known_at(now)
        holdings = {p.symbol for p in snapshot.positions} if snapshot else set()
        with self.store._connect() as db:
            rows = {r[0]: r for r in db.execute("SELECT * FROM research_universe").fetchall()}
        total = (sum((p.quantity*p.avg_cost for p in snapshot.positions), Decimal(0)) +
                 sum((a.cash for a in snapshot.accounts), Decimal(0))) if snapshot else None
        output = []
        for symbol in sorted(holdings | set(rows)):
            r = rows.get(symbol)
            thesis = self.store.get_thesis(symbol, now)
            cost = sum((p.quantity*p.avg_cost for p in snapshot.positions if p.symbol == symbol), Decimal(0)) if snapshot else None
            output.append(UniverseEntry(symbol=symbol, state='HOLDING' if symbol in holdings else r[1],
                created_at=datetime.fromisoformat(r[2]) if r else snapshot.source.imported_at,
                updated_at=datetime.fromisoformat(r[3]) if r else snapshot.source.imported_at,
                reason=r[4] if r else 'Authoritative portfolio holding', owner_note=r[5] if r else '',
                last_attention_cutoff=datetime.fromisoformat(r[6]) if r and r[6] else None,
                snapshot_id=snapshot.snapshot_id if snapshot else None,
                cost_exposure_pct=(cost*100/total).quantize(Decimal('.01')) if total and symbol in holdings else None,
                exposure_basis='COST_BASIS_PLUS_CASH' if total and symbol in holdings else 'UNKNOWN',
                thesis_version_id=thesis.thesis_version_id if thesis and thesis.status == 'USER_CONFIRMED' else None))
        return output

    def change(self, request: UniverseChange, now):
        entries = {e.symbol: e for e in self.entries(now)}
        if request.symbol in entries and entries[request.symbol].state == 'HOLDING':
            raise TDBError('UNIVERSE_HOLDING_IS_PORTFOLIO_DERIVED', 409)
        with self.store._connect() as db:
            db.execute("INSERT INTO research_universe VALUES (?,?,?,?,?,?,NULL) ON CONFLICT(symbol) DO UPDATE SET "
                "state=excluded.state,updated_at=excluded.updated_at,reason=excluded.reason,owner_note=excluded.owner_note",
                (request.symbol, request.state, utc(now).isoformat(), utc(now).isoformat(), request.reason, request.owner_note))
        return next(e for e in self.entries(now) if e.symbol == request.symbol)

    @staticmethod
    def checked(raw, digest):
        import json
        value = json.loads(raw)
        if content_hash(value) != digest:
            raise TDBError('ATTENTION_HISTORY_INVALID', 409)
        return value

    def report(self, scan_id):
        with self.store._connect() as db:
            r = db.execute("SELECT status,payload,payload_hash FROM attention_scans WHERE scan_id=?", (scan_id,)).fetchone()
        if not r:
            raise TDBError('ATTENTION_SCAN_NOT_FOUND', 404)
        return {'scan_id': scan_id, 'status': r[0], 'report': self.checked(r[1], r[2]) if r[1] else None}

    def queue(self, now, limit=5):
        from .attention_groups import review_units
        return review_units(self, now)[:limit]

    def deep_input(self, item_id, now):
        from .attention_groups import freeze_row
        row = next((r for r in self.queue(now, None) if r['item']['item_id'] == item_id), None)
        if not row:
            raise TDBError('ATTENTION_DEEP_REQUIRES_TOP_QUEUE_ITEM', 409)
        return freeze_row(self, row)

    def item(self, item_id):
        from .attention import AttentionItem
        with self.store._connect() as db:
            r = db.execute("SELECT payload,payload_hash FROM attention_items WHERE item_id=?", (item_id,)).fetchone()
        if not r:
            raise TDBError('ATTENTION_ITEM_NOT_FOUND', 404)
        return AttentionItem.model_validate(self.checked(*r))

    def latest_deep(self, item_id):
        with self.store._connect() as db:
            r = db.execute("SELECT deep_id,status,payload,payload_hash FROM attention_deep WHERE item_id=? "
                           "ORDER BY started_at DESC,rowid DESC LIMIT 1", (item_id,)).fetchone()
        return {'deep_id': r[0], 'status': r[1], 'result': self.checked(r[2],r[3]) if r[2] else None} if r else None

    def recover_interrupted(self):
        # Same single-process deployment boundary as the existing Decision jobs.
        # Evidence/backlog persists; restart never claims an interrupted model ran.
        with self.store._connect() as db:
            db.execute("UPDATE query_runs SET status='error' WHERE run_id IN "
                       "(SELECT scan_id FROM attention_scans WHERE status IN ('RUNNING','FAILED','INTERRUPTED')) AND status='running'")
            db.execute("UPDATE attention_scans SET status='INTERRUPTED' WHERE status='RUNNING'")
            db.execute("UPDATE attention_deep SET status='INTERRUPTED' WHERE status='RUNNING'")
