"""Isolate module-level app construction before pytest imports any test module."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

# Module-level app creation must never load Owner protected files/configuration.
for _optional_key in ('MIA_RESEARCH_MCP','MIA_LONGBRIDGE_CREDENTIALS',
        'LONGBRIDGE_MCP_ACCESS_TOKEN','LONGBRIDGE_AGENT_AUTH_CODE','MIA_SEC_RETRIEVAL','MIA_EMBEDDING_MODEL_DIR',
        'MIA_CAPITAL_REFERENCE_MODE','MIA_CAPITAL_REFERENCE_DIR'):
    os.environ.pop(_optional_key,None)

# create_app is also instantiated at main-module import. Focused pytest invocations
# must be safe without the canonical PowerShell wrapper or caller environment.
_import_database = TemporaryDirectory(prefix='mia-pytest-import-',
    dir=os.environ.get('MIA_ISOLATED_ROOT'), ignore_cleanup_errors=True)
os.environ['MIA_DB_PATH'] = str(Path(_import_database.name) / 'module-import.db')
from market_agent.test_isolation import isolated_database
assert isolated_database(os.environ['MIA_DB_PATH']).parent == Path(_import_database.name).resolve()


def pytest_configure(config):
    root = os.environ.get('MIA_ISOLATED_ROOT')
    if root:
        # The historical ini addopts names data/pytest_tmp. The explicit owned
        # root takes precedence before pytest creates any per-test resources.
        boundary = Path(root).resolve()
        selected = Path(config.option.basetemp).resolve() if config.option.basetemp else None
        # Honor an explicit owned path, including a nested pytest's own child
        # directory. Never make a nested session delete the parent's basetemp.
        if selected is None or not selected.is_relative_to(boundary) or selected == boundary:
            config.option.basetemp = str(boundary / ('pytest-' + uuid4().hex))
        else:
            selected.parent.mkdir(parents=True, exist_ok=True)
    elif config.option.basetemp:
        # A fresh clone has no data/ directory; pytest only creates the leaf.
        Path(config.option.basetemp).resolve().parent.mkdir(parents=True, exist_ok=True)
