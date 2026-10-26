"""The public export must be import-closed, and the gate must catch regressions.

v0.8.1 shipped 163 allowlisted files that could not be imported: the entrypoints
reached 22 `market_agent` modules the allowlist never named, so a clean clone died on
`ModuleNotFoundError`. These tests are the reason that cannot recur silently. The
positive test pins the real repository; the negative tests prove the gate actually
fails when a dependency, a lazily-imported module or a static asset goes missing.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location('check_import_closure',
                                                  ROOT / 'release/check_import_closure.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


closure = _load()


def sources(**modules):
    """Build a synthetic `src/market_agent` source map from keyword module bodies."""
    return {f'src/market_agent/{name}.py': body for name, body in modules.items()}


# ------------------------------------------------------------------ real repo

def test_repository_allowlist_is_import_closed():
    """The committed allowlist must satisfy the closure of the real entrypoints."""
    report = closure.verify(ROOT)
    assert report['status'] == 'PASS', json.dumps(
        {'missing_imports': report['missing_imports'],
         'missing_resources': report['missing_resources'],
         'chains': report['import_chains']}, indent=2)
    assert report['closure_size'] > 50


def test_entrypoints_are_allowlisted():
    allowlist = {e['path'] for e in closure.load_allowlist(ROOT)}
    for module in closure.DEFAULT_ENTRYPOINTS:
        assert f"src/market_agent/{module.split('.')[-1]}.py" in allowlist


def test_every_allowlisted_source_exists_at_head():
    """A stale allowlist path would silently export nothing and break the closure.

    Compared on `source`, not `path`: the allowlist renames on export (`release/LICENSE`
    becomes `LICENSE`), so `path` is not expected to exist in the private tree.
    """
    import subprocess
    tracked = set(subprocess.run(['git', '-C', str(ROOT), 'ls-files'],
                                 capture_output=True, text=True, check=True).stdout.split())
    stale = sorted({e['source'] for e in closure.load_allowlist(ROOT)} - tracked)
    assert stale == [], f'allowlist reads files that are not tracked: {stale}'


# ------------------------------------------------------- static gate: negatives

def test_missing_transitive_dependency_is_reported_with_its_chain():
    tree = sources(
        main='from .service import run\n',
        service='from .runtime_budget import limit\n',
        runtime_budget='LIMIT = 1\n',
    )
    report = closure.check_static(tree, {'src/market_agent/main.py',
                                         'src/market_agent/service.py'})
    assert report['status'] == 'FAIL'
    assert report['missing_imports'] == ['src/market_agent/runtime_budget.py']
    chain = report['import_chains']['src/market_agent/runtime_budget.py']
    # Every exported module is a root, so the chain may start at `service`; what matters
    # is that it ends at the missing module and names the path that reached it.
    assert chain[-1] == 'runtime_budget'
    assert any('service' in step for step in chain)


def test_import_inside_a_function_body_is_still_a_dependency():
    """`llm.production_llm_from_environment` imports runtime_budget lazily; a
    top-level-only scan would have called this file closed when it was not."""
    tree = sources(
        main='from .llm import build\n',
        llm='def build():\n    from .runtime_budget import limit\n    return limit\n',
        runtime_budget='limit = 1\n',
    )
    report = closure.check_static(tree, {'src/market_agent/main.py',
                                         'src/market_agent/llm.py'})
    assert report['missing_imports'] == ['src/market_agent/runtime_budget.py']


def test_package_qualified_import_is_detected():
    tree = sources(main='import market_agent.runtime_errors as e\n',
                   runtime_errors='VALUE = 1\n')
    report = closure.check_static(tree, {'src/market_agent/main.py'})
    assert report['missing_imports'] == ['src/market_agent/runtime_errors.py']


def test_importing_a_symbol_directly_from_the_package_is_detected():
    tree = sources(main='from . import clock\n', clock='NOW = 1\n')
    report = closure.check_static(tree, {'src/market_agent/main.py'})
    assert report['missing_imports'] == ['src/market_agent/clock.py']


def test_partially_exported_static_directory_is_rejected():
    """`main` mounts the whole `static/` directory; exporting some of it renders a
    page whose scripts 404, which no import test would notice."""
    tree = sources(main='from pathlib import Path\nSTATIC = Path(__file__).parent / "static"\n')
    assets = {'src/market_agent/static/index.html', 'src/market_agent/static/today.js'}
    report = closure.check_static(tree, {'src/market_agent/main.py',
                                         'src/market_agent/static/index.html'},
                                  assets=assets)
    assert report['status'] == 'FAIL'
    assert report['missing_resources'] == ['src/market_agent/static/today.js']


def test_asset_directory_read_through_with_name_is_rejected_when_partial():
    """`public_demo` resolves assets via `Path(__file__).with_name('demo_assets')`."""
    tree = sources(
        public_demo="from pathlib import Path\nA = Path(__file__).with_name('demo_assets')\n")
    assets = {'src/market_agent/demo_assets/a.json', 'src/market_agent/demo_assets/b.json'}
    report = closure.check_static(tree, {'src/market_agent/public_demo.py',
                                         'src/market_agent/demo_assets/a.json'},
                                  assets=assets)
    assert report['missing_resources'] == ['src/market_agent/demo_assets/b.json']


def test_a_closed_allowlist_passes():
    tree = sources(main='from .service import run\n', service='run = 1\n')
    report = closure.check_static(tree, {'src/market_agent/main.py',
                                         'src/market_agent/service.py'})
    assert report['status'] == 'PASS' and report['missing_imports'] == []


# ------------------------------------------------------ runtime gate: negatives

def tree_at(root, **modules):
    """Write `src/market_agent/<name>.py` for each keyword, keeping `__init__` exact.

    Uses explicit path segments: `'__init__'.replace('__', '/')` yields `/init/`, which
    joins to a drive-relative path and writes outside the temp tree.
    """
    for name, body in modules.items():
        path = root / 'src/market_agent' / f'{name}.py'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')
    return root


def test_runtime_gate_passes_a_closed_export(tmp_path):
    tree_at(tmp_path, __init__='', a='from .b import VALUE\nVALUE = 1\n', b='VALUE = 2\n')
    report = closure.check_runtime(tmp_path)
    assert report['status'] == 'PASS', report
    assert report['imported'] == 2
    assert Path(report['package_file']).parent == (tmp_path / 'src/market_agent').resolve()


def test_runtime_gate_rejects_a_missing_module_even_when_the_file_list_looks_right(tmp_path):
    """The exact v0.8.1 failure: allowlist satisfied, import still impossible."""
    tree_at(tmp_path, __init__='', a='from .runtime_budget import limit\n')
    report = closure.check_runtime(tmp_path)
    assert report['status'] == 'FAIL'
    assert any('runtime_budget' in e for e in report['errors'])


def test_runtime_gate_rejects_a_package_resolved_outside_the_export(tmp_path):
    """An editable `.pth` in the ambient interpreter must not satisfy the gate.

    Judged on a real probe result: the package resolved from an ambient tree while the
    export's own `src` was expected.
    """
    ambient = (tmp_path / 'ambient/src/market_agent').resolve()
    export_src = tmp_path / 'export/src'
    export_src.mkdir(parents=True)
    errors = closure.judge_import_result(
        {'package_file': str(ambient / '__init__.py'), 'failures': [], 'imported': 1},
        export_src)
    assert errors and 'resolved outside export' in errors[0]


def test_runtime_gate_accepts_a_package_resolved_inside_the_export(tmp_path):
    export_src = tmp_path / 'export/src'
    (export_src / 'market_agent').mkdir(parents=True)
    errors = closure.judge_import_result(
        {'package_file': str(export_src / 'market_agent/__init__.py'),
         'failures': [], 'imported': 3}, export_src)
    assert errors == []


def test_runtime_probe_blocks_network_during_import(tmp_path):
    """0 unauthorized external requests: importing the export must not open a socket.

    The fixture module raises at import time if a real connection succeeds, so this
    test fails loudly if the probe's socket guard is ever removed.
    """
    tree_at(tmp_path, __init__='',
            net="import socket\n"
                "try:\n"
                "    socket.create_connection(('example.com', 80), timeout=0.1)\n"
                "except OSError:\n"
                "    BLOCKED = True\n"
                "else:\n"
                "    raise AssertionError('network was reachable during import')\n")
    report = closure.check_runtime(tmp_path)
    assert report['status'] == 'PASS', report
    assert report['network'] == 'disabled during probe'


# ------------------------------------------------------------ allowlist emit

def test_emit_is_idempotent_and_covers_the_closure(tmp_path):
    """Re-emitting must not churn, so the allowlist can be regenerated safely."""
    tree_at(tmp_path, __init__='', main='from .service import run\n', service='run = 1\n')
    (tmp_path / 'release').mkdir()
    (tmp_path / 'release/public-files.json').write_text(
        json.dumps([{'source': 'src/market_agent/main.py',
                     'path': 'src/market_agent/main.py'}]), encoding='utf-8')
    first = closure.emit_allowlist(tmp_path)
    (tmp_path / 'release/public-files.json').write_text(json.dumps(first), encoding='utf-8')
    second = closure.emit_allowlist(tmp_path)
    assert first == second
    assert {e['path'] for e in first} == {'src/market_agent/main.py',
                                          'src/market_agent/service.py'}
    assert closure.verify(tmp_path)['status'] == 'PASS'
