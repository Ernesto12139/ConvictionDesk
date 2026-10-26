"""Prove a release allowlist is import-closed and resource-closed.

The v0.8.1 public export carried 163 allowlisted files but was not importable: the
entrypoints pulled in 22 `market_agent` modules that the allowlist never named, so a
clean clone raised `ModuleNotFoundError` on first import. Hand-listing files cannot
stay correct while the source keeps growing, so the allowlist is now *checked*
instead of trusted.

Two independent gates, because they fail for different reasons:

``static``
    Parse the allowlisted sources, follow every `import`/`from ... import` reachable
    from the declared entrypoints (including imports nested inside functions), and
    require the closure to be a subset of the allowlist. Also require any directory a
    module reads through ``Path(__file__).parent`` to be exported in full, since a
    half-exported `static/` renders a page with missing scripts.

``runtime``
    Import every exported module in a fresh interpreter with sockets disabled and
    assert the package resolved inside the exported tree. Static analysis can be
    satisfied by a file that still explodes at import time; this gate cannot.

Neither gate publishes, deletes or rewrites anything.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from collections import deque
from pathlib import Path, PurePosixPath

PACKAGE = "market_agent"
SRC_PREFIX = f"src/{PACKAGE}"
# Modules a clean clone must be able to start from. `serve` and `public_demo` are the
# two documented entrypoints; `main` builds the FastAPI app both of them use.
DEFAULT_ENTRYPOINTS = (f"{PACKAGE}.main", f"{PACKAGE}.serve", f"{PACKAGE}.public_demo")
# Directories read through `Path(__file__).parent / "<name>"` must be exported whole.
RESOURCE_DIRS = ("static", "demo_assets")

_RUNTIME_PROBE = r'''
import importlib, json, socket, sys

class _Blocked(OSError):
    pass

def _refuse(*args, **kwargs):
    raise _Blocked("network disabled by closure probe")

# `ssl` subclasses `socket.socket` at import time, so the class must stay a class.
# Block the connect methods and the resolution/creation helpers instead: every
# outbound connection passes through one of them.
class _BlockedSocket(socket.socket):
    def connect(self, *args, **kwargs):
        _refuse()
    def connect_ex(self, *args, **kwargs):
        _refuse()

socket.socket = _BlockedSocket
socket.create_connection = _refuse
socket.getaddrinfo = _refuse

request = json.loads(sys.argv[1])
failures, imported = [], []
for name in request["modules"]:
    try:
        module = importlib.import_module(name)
        imported.append(name)
    except BaseException as error:  # noqa: BLE001 - report, never abort the probe
        failures.append({"module": name, "error": type(error).__name__,
                         "detail": str(error)[:200]})

package = importlib.import_module("market_agent")
print(json.dumps({
    "package_file": getattr(package, "__file__", None),
    "imported": len(imported),
    "failures": failures,
}))
'''


# --------------------------------------------------------------------------- git

def _git_batch(repo: Path, names: list[str], rev: str = "HEAD") -> dict[str, bytes]:
    """Read many blobs through one `git cat-file --batch` process.

    Per-blob `git cat-file` spawns a process per file and made a 163-file export take
    two minutes on Windows; the batch reader keeps verification cheap enough to run
    inside a test.
    """
    if not names:
        return {}
    payload = "".join(f"{rev}:{name}\n" for name in names).encode()
    # `--batch` accepts a `rev:path` query, so this works for any commit, not just HEAD.
    out = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"],
                         input=payload, capture_output=True, check=True).stdout
    blobs, offset = {}, 0
    for name in names:
        newline = out.index(b"\n", offset)
        header = out[offset:newline].decode()
        offset = newline + 1
        if header.endswith("missing"):
            raise ValueError(f"allowlisted path missing from source: {name}")
        _, _, size = header.split()
        size = int(size)
        blobs[name] = out[offset:offset + size]
        offset += size + 1  # trailing newline after each blob
    return blobs


def read_tree(repo: Path, sha: str | None = None) -> dict[str, bytes]:
    """Every file under `src/market_agent` (sources *and* assets), from a commit."""
    if sha:
        names = subprocess.run(
            ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", sha, "--", SRC_PREFIX],
            capture_output=True, check=True, text=True).stdout.split()
        return _git_batch(repo, names, sha)
    root = repo / SRC_PREFIX
    return {p.relative_to(repo).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def read_sources(repo: Path, sha: str | None = None) -> dict[str, str]:
    """Every `market_agent` Python source file as text."""
    return {name: data.decode("utf-8")
            for name, data in read_tree(repo, sha).items() if name.endswith(".py")}


# ----------------------------------------------------------------- static gate

def imported_modules(source: str, *, module: str) -> set[str]:
    """Every intra-package module this source reaches, including lazy imports.

    Nested imports matter: `llm.production_llm_from_environment` imports
    `runtime_budget` inside the function body, which is exactly how a module can be
    missing from the allowlist while every top-level import looks satisfied.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.level:  # relative: `from .x import y` / `from . import x`
                if node.module:
                    found.add(node.module.split(".")[0])
                else:
                    found.update(alias.name.split(".")[0] for alias in node.names)
            elif node.module and node.module.split(".")[0] == PACKAGE:
                parts = node.module.split(".")
                if len(parts) > 1:
                    found.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if len(parts) > 1 and parts[0] == PACKAGE:
                    found.add(parts[1])
    return {name for name in found if name}


def resource_dirs_used(source: str) -> set[str]:
    """Resource directories a module reads relative to its own file.

    Covers both spellings the codebase uses: ``Path(__file__).parent / "static"`` and
    ``Path(__file__).with_name('demo_assets')``. Missing the second form would let a
    half-exported asset directory pass the gate.
    """
    used = set()
    for node in ast.walk(ast.parse(source)):
        # `Path(__file__).parent / "static"`
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            right = node.right
            if isinstance(right, ast.Constant) and right.value in RESOURCE_DIRS:
                used.add(right.value)
        # `Path(__file__).with_name('demo_assets')`
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == 'with_name' and node.args):
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and arg.value in RESOURCE_DIRS:
                used.add(arg.value)
    return used


def static_closure(sources: dict[str, str], *, entrypoints=DEFAULT_ENTRYPOINTS,
                   extra_roots=()) -> dict:
    """Breadth-first closure over the declared entrypoints plus any extra roots.

    `extra_roots` matters because the export ships modules that no entrypoint imports
    (public preview surfaces, for example). Those still have to import cleanly, so every
    exported module is treated as a root rather than only what `main` happens to reach.
    """
    by_module = {PurePosixPath(name).stem: name for name in sources}
    reached: dict[str, list[str]] = {}  # module -> import chain, for actionable errors
    roots = list(entrypoints) + [f"{PACKAGE}.{PurePosixPath(r).stem}" for r in extra_roots]
    queue = deque((name, [name]) for name in roots)
    while queue:
        module, chain = queue.popleft()
        short = module.split(".")[-1]
        if short in reached or short not in by_module:
            continue
        reached[short] = chain
        for dependency in sorted(imported_modules(sources[by_module[short]], module=module)):
            if dependency not in reached:
                queue.append((f"{PACKAGE}.{dependency}", chain + [dependency]))
    paths = {by_module[m] for m in reached}
    resources = set()
    for module in reached:
        resources |= resource_dirs_used(sources[by_module[module]])
    return {"modules": reached, "paths": paths, "resource_dirs": resources}


def check_static(sources: dict[str, str], allowlist: set[str], *,
                 entrypoints=DEFAULT_ENTRYPOINTS, assets: set[str] = frozenset()) -> dict:
    exported_sources = {p for p in allowlist if p.startswith(SRC_PREFIX) and p.endswith(".py")}
    closure = static_closure(sources, entrypoints=entrypoints, extra_roots=exported_sources)
    missing_imports = sorted(closure["paths"] - allowlist)
    # A resource directory a module reads must be exported in full, not by guesswork.
    missing_resources = []
    for directory in sorted(closure["resource_dirs"]):
        prefix = f"{SRC_PREFIX}/{directory}/"
        present = {name for name in assets if name.startswith(prefix)}
        missing_resources.extend(sorted(present - allowlist))
    chains = {path: closure["modules"][PurePosixPath(path).stem]
              for path in missing_imports}
    return {
        "status": "PASS" if not missing_imports and not missing_resources else "FAIL",
        "closure_size": len(closure["paths"]),
        "allowlisted_sources": len({p for p in allowlist if p.startswith(SRC_PREFIX)}),
        "missing_imports": missing_imports,
        "missing_resources": missing_resources,
        "import_chains": chains,
        "resource_dirs": sorted(closure["resource_dirs"]),
    }


# ---------------------------------------------------------------- runtime gate

def judge_import_result(result: dict, src: Path) -> list[str]:
    """Turn one probe result into errors, including the resolution-location guard.

    The location check is what stops an editable `.pth` or an ambient site-packages
    copy from making a broken export look importable on the machine that built it.
    """
    errors = []
    package_file = Path(result.get("package_file") or "").resolve()
    if src.resolve() not in package_file.parents:
        errors.append(f"package resolved outside export: {package_file}")
    for failure in result.get("failures", []):
        errors.append(f"{failure['module']}: {failure['error']}: {failure['detail']}")
    return errors


def check_runtime(tree: Path, *, python: str | None = None, timeout: int = 180) -> dict:
    """Import every exported module with sockets disabled, inside the export only."""
    src = tree / "src"
    modules = sorted(f"{PACKAGE}.{p.stem}" for p in (src / PACKAGE).glob("*.py")
                     if p.stem != "__init__")
    # Copy the ambient environment (Windows needs SystemRoot to spawn at all), then
    # pin import resolution to the export and drop anything that could redirect it.
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}}
    env.update({"PYTHONPATH": str(src), "PYTHONDONTWRITEBYTECODE": "1"})
    completed = subprocess.run(
        [python or sys.executable, "-c", _RUNTIME_PROBE, json.dumps({"modules": modules})],
        capture_output=True, text=True, timeout=timeout, env=env, cwd=str(tree))
    if completed.returncode != 0:
        return {"status": "FAIL", "stage": "probe", "stderr": completed.stderr[-1500:],
                "modules": len(modules)}
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    errors = judge_import_result(result, src)
    return {
        "status": "PASS" if not errors else "FAIL",
        "modules": len(modules),
        "imported": result["imported"],
        "errors": errors,
        "package_file": str(Path(result["package_file"] or "").resolve()),
        "network": "disabled during probe",
    }


# ---------------------------------------------------------------------- driver

def load_allowlist(repo: Path, sha: str | None = None) -> list[dict]:
    if sha:
        raw = subprocess.run(
            ["git", "-C", str(repo), "show", f"{sha}:release/public-files.json"],
            capture_output=True, check=True, text=True).stdout
    else:
        raw = (repo / "release/public-files.json").read_text(encoding="utf-8")
    return json.loads(raw)


def emit_allowlist(repo: Path, *, sha: str | None = None,
                   entrypoints=DEFAULT_ENTRYPOINTS) -> list[dict]:
    """Union of the current allowlist with everything the closure requires.

    Deterministic and idempotent: re-running after the source grows only adds the
    newly reachable files, so the allowlist cannot silently drift out of closure
    again. Existing entries keep their curated `source`/`path` mapping.
    """
    entries = load_allowlist(repo, sha)
    merged = {entry["path"]: {"source": entry["source"], "path": entry["path"]}
              for entry in entries}
    tree = read_tree(repo, sha)
    sources = {name: data.decode("utf-8")
               for name, data in tree.items() if name.endswith(".py")}
    closure = static_closure(sources, entrypoints=entrypoints,
                             extra_roots={e["path"] for e in entries})
    required = set(closure["paths"])
    for directory in closure["resource_dirs"]:
        prefix = f"{SRC_PREFIX}/{directory}/"
        required |= {name for name in tree if name.startswith(prefix)}
    for name in sorted(required - set(merged)):
        merged[name] = {"source": name, "path": name}
    return [merged[path] for path in sorted(merged)]


def verify(repo: Path, *, sha: str | None = None, entrypoints=DEFAULT_ENTRYPOINTS) -> dict:
    """Static gate over a commit or working tree. Returns a machine-readable report."""
    entries = load_allowlist(repo, sha)
    allowlist = {entry["path"] for entry in entries}
    tree = read_tree(repo, sha)
    sources = {name: data.decode("utf-8")
               for name, data in tree.items() if name.endswith(".py")}
    assets = {name for name in tree if not name.endswith(".py")}
    report = check_static(sources, allowlist, entrypoints=entrypoints, assets=assets)
    report["allowlist_entries"] = len(entries)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=Path("."))
    parser.add_argument("--sha", help="verify the allowlist at an exact commit")
    parser.add_argument("--tree", type=Path, help="also run the runtime gate on an export")
    parser.add_argument("--python", help="interpreter for the runtime gate")
    parser.add_argument("--emit-allowlist", metavar="PATH",
                        help="write the closure-complete allowlist here instead of checking")
    args = parser.parse_args(argv)

    if args.emit_allowlist:
        entries = emit_allowlist(args.source, sha=args.sha)
        Path(args.emit_allowlist).write_text(json.dumps(entries, indent=2) + "\n",
                                             encoding="utf-8")
        print(json.dumps({"status": "EMITTED", "entries": len(entries),
                          "path": args.emit_allowlist}))
        return 0

    report = verify(args.source, sha=args.sha)
    if args.tree:
        report["runtime"] = check_runtime(args.tree, python=args.python)
        if report["runtime"]["status"] != "PASS":
            report["status"] = "FAIL"
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
