"""Shared, synthetic, read-only replay on the production Console and APIs.

No visitor identity is implied. New research and every other mutation are denied
before routing. The packaged dataset is the only import source; environment keys
and Owner database paths never select providers or data in this mode.
"""
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi.responses import JSONResponse

from .llm import UnconfiguredLLMClient
from .persistence import Store
from .provider import MarketDataResult, ProviderError
from .research_task import ResearchTasks
from .tdb import TDBError, content_hash


ASSETS = Path(__file__).with_name('demo_assets')
DATASET = ASSETS / 'research-replay-v2.json'
READ_ROUTES = re.compile(
    r'^(?:/|/health|/api/console/status|/api/console/portfolio-overview|'
    r'/api/portfolio/current|/api/research-universe|/api/attention/queue|/api/attention/items/(?:sha256:)?[a-f0-9]{64}|'
    r'/api/public-demo|/api/research/tasks(?:/[^/]+(?:/(?:revisions(?:/[1-9][0-9]*)?|valuation))?)?|'
    r'/static/[a-zA-Z0-9_-]+\.(?:js|css|svg))$')
# SQLite-maintained FTS shadow tables are rebuilt by the schema, never replayed.
GENERATED_TABLES = ('sqlite_', 'sec_chunks_fts')


def dump_tables(db):
    """Every replayed row, in a stable order; the comparison unit for integrity."""
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
             "AND sql NOT LIKE 'CREATE VIRTUAL%' ORDER BY name")]
    tables = {}
    for name in names:
        if name.startswith(GENERATED_TABLES):
            continue
        quoted = '"'+name.replace('"', '""')+'"'
        columns = [r[1] for r in db.execute('PRAGMA table_info('+quoted+')')]
        rows = sorted(([*row] for row in db.execute('SELECT * FROM '+quoted)), key=json.dumps)
        if rows:
            tables[name] = {'columns': columns, 'rows': rows}
    return tables


class ReplayMarket:
    name = 'public-demo-synthetic-replay'

    def __init__(self, payload):
        self.result = MarketDataResult.model_validate(payload)

    def retrieve_eod(self, symbols, cutoff):
        observations = [o for o in self.result.observations if o.symbol in symbols and o.as_of <= cutoff]
        ids = {o.evidence_id for o in observations}
        return MarketDataResult(observations=observations,
            evidence=[e for e in self.result.evidence if e.evidence_id in ids])


class OfflineCompany:
    name = 'offline-replay-only'

    def search(self, *args, **kwargs):
        raise ProviderError('configuration', 'Public demo cannot retrieve external data')


def load_dataset():
    dataset = json.loads(DATASET.read_text(encoding='utf-8'))
    if dataset.get('version') != 2 or dataset.get('synthetic') is not True:
        raise ValueError('Public demo requires the packaged synthetic dataset')
    if content_hash(dataset['data']) != dataset['content_hash']:
        raise ValueError('Public demo dataset integrity failed')
    return dataset


def validate_database(path, dataset):
    """Validate existing files read-only BEFORE Store can initialize schemas.

    Every non-generated table must equal the packaged replay exactly, so a
    visitor, tampering or a stray background job cannot add or alter history.
    """
    try:
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Public demo database integrity failed')
            tables = dump_tables(db)
            for name in ('research_tasks', 'research_task_turns'):
                table = tables.get(name, {'columns': [], 'rows': []})
                index = {key: table['columns'].index(key) for key in ('payload_hash', 'payload')} if table['rows'] else {}
                for row in table['rows']:
                    if content_hash(json.loads(row[index['payload']])) != row[index['payload_hash']]:
                        raise TDBError('RESEARCH_TASK_INTEGRITY', 409)
    except sqlite3.DatabaseError:
        raise ValueError('Public demo database is not the packaged replay') from None
    if tables != dataset['data']['tables']:
        raise ValueError('Public demo contains data outside the packaged replay')


def seed_database(folder, dataset):
    folder = Path(folder)
    if folder.is_symlink() or folder.is_junction():
        raise ValueError('Public demo data directory cannot be a link')
    folder = folder.resolve()
    marker = folder / 'public-demo.json'
    if marker.is_symlink() or marker.exists() and marker.stat().st_nlink != 1:
        raise ValueError('Public demo marker cannot be a link')
    identity = {'mode': 'PUBLIC_DEMO', 'dataset_hash': dataset['content_hash']}
    if folder.exists() and any(folder.iterdir()):
        if not marker.is_file() or json.loads(marker.read_text(encoding='utf-8')) != identity:
            raise ValueError('Refusing non-demo or different-version data directory')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'public-demo.sqlite3'
    if path.is_symlink() or marker.is_symlink() or path.exists() and path.stat().st_nlink != 1:
        raise ValueError('Public demo data files cannot be links')
    if path.exists():
        validate_database(path, dataset)
    if not marker.exists():
        # An initialization failure is fail-closed on restart, never permission
        # to load an unrelated existing SQLite file.
        marker.write_text(json.dumps(identity), encoding='utf-8')
    store = Store(path)
    from .research_universe import ResearchStore
    # Same schema owners as the running app; neither creates rows.
    ResearchStore(store)
    tasks = ResearchTasks(store)
    if store.get_snapshot() is None:
        # Seed the frozen rows byte-for-byte, never a new-version serialization.
        with store._connect() as db:
            # Foreign keys are checked once at commit, so the alphabetical
            # table order of the frozen dump cannot weaken referential checks.
            db.execute('PRAGMA defer_foreign_keys=ON')
            for name, table in dataset['data']['tables'].items():
                quoted = '"'+name.replace('"', '""')+'"'
                columns = ','.join('"'+c.replace('"', '""')+'"' for c in table['columns'])
                marks = ','.join('?' for _ in table['columns'])
                db.executemany(f'INSERT OR IGNORE INTO {quoted} ({columns}) VALUES ({marks})', table['rows'])
    # Check every saved row on every restart, not just a flag.
    validate_database(path, dataset)
    for example in dataset['data']['examples']:
        tasks.revision(example['task_id'], example['revision'])
    return store


def create_public_demo(create_app, environment):
    execution = environment.get('MIA_RESEARCH_EXECUTION', 'OFFLINE_REPLAY')
    if execution != 'OFFLINE_REPLAY':
        raise ValueError('INVITED_LIVE_RESEARCH is disabled until server identity and hard budgets are implemented')
    dataset = load_dataset()
    folder = environment.get('MIA_DEMO_DIR', 'data/public-demo')
    store = seed_database(folder, dataset)
    cutoff = datetime.fromisoformat(dataset['data']['cutoff'])
    company = OfflineCompany()
    app = create_app(db_path=store.path, market_provider=ReplayMarket(dataset['data']['market']),
        llm_client=UnconfiguredLLMClient(), news_provider=company, sec_provider=company,
        clock=lambda: cutoff, environment={})
    manifest = {'runtime_mode': 'PUBLIC_DEMO', 'execution_mode': 'OFFLINE_REPLAY',
        'access': 'SHARED_READ_ONLY', 'synthetic': True, 'live_research_enabled': False,
        'model_budget_usd': '0', 'dataset_hash': dataset['content_hash'],
        'as_of': cutoff.isoformat(), 'examples': dataset['data']['examples'],
        'dataset_version': dataset['version'], 'scan_outcome': dataset['data']['scan_outcome'],
        'live_freshness': 'NOT_APPLICABLE_SYNTHETIC', 'issuers': 'FICTIONAL'}
    revision = environment.get('MIA_BUILD_REVISION', '')
    manifest['build_revision'] = revision if re.fullmatch('[a-f0-9]{40}', revision) else 'development'
    app.state.public_demo = manifest

    @app.get('/api/public-demo')
    def public_manifest():
        return manifest

    @app.middleware('http')
    async def public_boundary(request, call_next):
        # No request body is read and no endpoint/background job executes first.
        if request.method not in {'GET', 'HEAD'}:
            response = JSONResponse(status_code=403, content={'detail': 'PUBLIC_DEMO_READ_ONLY'})
        elif not READ_ROUTES.fullmatch(request.url.path):
            response = JSONResponse(status_code=404, content={'detail': 'PUBLIC_DEMO_ROUTE_NOT_AVAILABLE'})
        else:
            response = await call_next(request)
        response.headers['X-MIA-Mode'] = 'PUBLIC_DEMO; OFFLINE_REPLAY; SYNTHETIC'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    return app
