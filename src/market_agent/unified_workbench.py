"""One existing Console implementation, isolated synthetic/empty-owner preview scopes.

This candidate never opens the real Owner database or enables provider dispatch.
"""
from contextlib import AsyncExitStack, asynccontextmanager
from enum import Enum
import importlib
import json
import os
from pathlib import Path
import socket

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

COOKIE = 'convictiondesk_preview_scope'
MARKER = 'UNIFIED_PREVIEW_ONLY.json'


class Scope(str, Enum):
    SYNTHETIC = 'SYNTHETIC'
    OWNER_PREVIEW = 'OWNER_PREVIEW'
    OWNER_LOCAL = 'OWNER_LOCAL'


class SelectScope(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope: Scope


def checked_root(root):
    requested = Path(root).absolute()
    for parent in (requested, *requested.parents):
        if parent.is_symlink() or getattr(parent, 'is_junction', lambda: False)():
            raise ValueError('PREVIEW_LINK_REFUSED')
    root = requested.resolve()
    marker = root / MARKER
    if marker.is_symlink():
        raise ValueError('PREVIEW_LINK_REFUSED')
    if root.exists() and any(root.iterdir()):
        if not marker.is_file() or json.loads(marker.read_text()) != {'kind': 'SYNTHETIC_AND_EMPTY_OWNER_ONLY', 'version': 1}:
            raise ValueError('PREVIEW_UNMARKED_ROOT_REFUSED')
    root.mkdir(parents=True, exist_ok=True)
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            entry = Path(directory) / name
            if entry.is_symlink() or getattr(entry, 'is_junction', lambda: False)():
                raise ValueError('PREVIEW_LINK_REFUSED')
    if not marker.exists():
        marker.write_text(json.dumps({'kind': 'SYNTHETIC_AND_EMPTY_OWNER_ONLY', 'version': 1}), encoding='utf-8')
    return root


def _main_factory(root):
    # main.py has a historical module-level app. Contain that import's default
    # database and environment too; never let it adopt the caller's Owner path.
    saved = dict(os.environ)
    keep = {'SYSTEMROOT','WINDIR','PATH','PATHEXT','COMSPEC','TEMP','TMP'}
    environment = {k:v for k,v in saved.items() if k.upper() in keep}
    environment.update(MIA_RUNTIME_MODE='OWNER', MIA_PROVIDER_HUB='0',
                       MIA_DB_PATH=str(root / 'module-import.sqlite3'))
    try:
        os.environ.clear()
        os.environ.update(environment)
        return importlib.import_module('market_agent.main').create_app
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _selection(request, default=Scope.SYNTHETIC):
    try:
        return Scope(request.cookies.get(COOKIE, default.value))
    except ValueError:
        raise HTTPException(400, detail='PREVIEW_SCOPE_INVALID') from None


def _context(scope):
    if scope == Scope.OWNER_LOCAL:
        return {'scope': scope.value, 'paid_dispatch': 'REQUIRES_SCOPED_ADMISSION',
                'provider_hub_enabled': True, 'real_owner_database_opened': True,
                'owner_data_integration': 'LOCAL_QUALIFIED_SLICE',
                'model_kind': 'NATIVE_GATED_NOT_VERIFIED', 'data_kind': 'OWNER_LOCAL',
                'owner_acceptance': 'NOT_ASSESSED'}
    return {'scope': scope.value, 'paid_dispatch': 'BLOCKED', 'provider_hub_enabled': False,
            'real_owner_database_opened': False, 'owner_data_integration': 'PLANNED_QODER_W_27',
            'model_kind': 'SCRIPTED' if scope == Scope.SYNTHETIC else 'NOT_CONFIGURED',
            'data_kind': 'REAL_PUBLIC_SEC_METADATA__SYNTHETIC_PORTFOLIO' if scope == Scope.SYNTHETIC else 'EMPTY_ISOLATED_SANDBOX',
            'owner_acceptance': 'NOT_ASSESSED_FOR_THIS_CANDIDATE'}


def _decorate(app, scope, *, owner_admitted=False):
    @app.middleware('http')
    async def mode_surface(request, call_next):
        response = await call_next(request)
        if request.url.path == '/' and response.status_code == 200:
            body = b''.join([part async for part in response.body_iterator]).decode('utf-8')
            panel = '''<section id="workbench-mode" class="workbench-mode" aria-label="工作空间模式">
<div><strong>同一研究工作台，明确的数据范围</strong><p id="workbench-mode-status" role="status"></p></div>
<div class="workbench-mode-actions"><button type="button" data-workbench-scope="SYNTHETIC">合成体验</button>
<button type="button" data-workbench-scope="OWNER_PREVIEW">Owner 接入预检</button></div>
<details class="workbench-boundary"><summary>数据与调用边界</summary><p class="small muted">接入预检是独立空白沙盒，尚未连接真实持仓或凭据。两种模式不复制或合并数据；付费调用保持阻断。</p></details></section>'''
            if owner_admitted:
                panel = panel.replace('data-workbench-scope="OWNER_PREVIEW">Owner 接入预检',
                                      'data-workbench-scope="OWNER_LOCAL">Owner 本机研究')
                panel = panel.replace('接入预检是独立空白沙盒，尚未连接真实持仓或凭据。两种模式不复制或合并数据；付费调用保持阻断。',
                    'Owner 模式读取本机授权组合；样本模式使用隔离合成数据。两者不复制或合并数据。真实调用仍须来源权限、服务器同意与预算准入。')
            if scope == Scope.OWNER_LOCAL:
                panel += '<section id="owner-value" class="panel" aria-label="持仓证据核查"><h2>持仓与披露核查</h2><p id="owner-value-status" role="status">正在读取本机证据…</p><div id="owner-value-content"></div></section>'
                body = body.replace('</head>', '<script src="/static/owner-value.js" defer></script></head>', 1)
            body = body.replace('</head>', '<link rel="stylesheet" href="/static/workbench-mode.css"><script src="/static/workbench-mode.js" defer></script><script src="/static/execution-trace.js" defer></script></head>', 1)
            body = body.replace('<main>', '<main>'+panel, 1)
            return HTMLResponse(body, headers={'Cache-Control': 'no-store'})
        response.headers['Cache-Control'] = 'no-store'
        return response


class ScopedConsole:
    def __init__(self, apps, default=Scope.SYNTHETIC):
        self.apps, self.default = apps, default

    async def __call__(self, scope, receive, send):
        request = Request(scope)
        try:
            selected = _selection(request, self.default)
        except HTTPException as error:
            return await JSONResponse({'detail': error.detail}, status_code=error.status_code)(scope, receive, send)
        if selected not in self.apps:
            return await JSONResponse({'detail':'OWNER_SCOPE_NOT_ADMITTED'},status_code=403)(scope, receive, send)
        # The Owner sandbox is preview-only, not a path to importing or modifying
        # real holdings. Even same-origin mutations are refused server-side.
        if selected == Scope.OWNER_PREVIEW and request.method not in {'GET','HEAD','OPTIONS'}:
            return await JSONResponse({'detail':'OWNER_SCOPE_NOT_ADMITTED'},status_code=409)(scope, receive, send)
        return await self.apps[selected](scope, receive, send)


def build_app(root, *, owner_app=None, default_scope=Scope.SYNTHETIC, trace_code=None):
    root = checked_root(root)
    factory = _main_factory(root)
    from .public_ivs import build_app as public_app
    owner_root = root / 'owner-preview'
    owner_root.mkdir(exist_ok=True)
    owner_marker = owner_root / 'EMPTY_OWNER_PREVIEW.json'
    database = owner_root / 'preview.sqlite3'
    if database.exists() and (not owner_marker.exists() or json.loads(owner_marker.read_text()) != {'kind':'EMPTY_PREVIEW_ONLY'}):
        raise ValueError('OWNER_PREVIEW_UNMARKED_DATABASE_REFUSED')
    owner_marker.write_text('{"kind":"EMPTY_PREVIEW_ONLY"}',encoding='utf-8')
    apps = {Scope.SYNTHETIC: public_app(root / 'synthetic'),
            Scope.OWNER_PREVIEW: factory(database, environment={'MIA_RUNTIME_MODE':'OWNER', 'MIA_PROVIDER_HUB':'0', 'MIA_RESEARCH_STRATEGY':'BASELINE_ONLY'})}
    from .workbench_trace import install_workbench_trace, workbench_code_identity
    install_workbench_trace(apps[Scope.SYNTHETIC], root / 'sample-traces',
                            trace_code or workbench_code_identity(), 'KEYLESS_SCRIPTED')
    if owner_app is not None:
        apps[Scope.OWNER_LOCAL] = owner_app
    if default_scope not in apps:
        raise ValueError('DEFAULT_SCOPE_NOT_ADMITTED')
    for scope, app in apps.items():
        _decorate(app, scope, owner_admitted=owner_app is not None)

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            for child in apps.values():
                await stack.enter_async_context(child.router.lifespan_context(child))
            yield

    app = FastAPI(title='ConvictionDesk', version='0.11.0', lifespan=lifespan)
    app.state.scopes = apps

    @app.middleware('http')
    async def loopback_host(request, call_next):
        if (request.url.hostname not in {'127.0.0.1','localhost','::1'}
                or request.client.host not in {'127.0.0.1','::1','testclient'}
                or any(k in request.headers for k in ('forwarded','x-forwarded-for','x-forwarded-host','x-forwarded-proto','x-real-ip'))):
            return JSONResponse({'detail':'LOCAL_PREVIEW_ONLY'},status_code=403)
        return await call_next(request)

    @app.get('/api/workbench/mode')
    def current_mode(request: Request):
        selected = _selection(request, default_scope)
        if selected not in apps:
            raise HTTPException(403, detail='OWNER_SCOPE_NOT_ADMITTED')
        return JSONResponse(_context(selected), headers={'Cache-Control':'no-store'})

    @app.post('/api/workbench/mode')
    def select_mode(payload: SelectScope, request: Request):
        if request.headers.get('origin') != str(request.base_url).rstrip('/') or request.headers.get('x-workbench-mode') != 'select':
            raise HTTPException(403, detail='ORIGIN_REJECTED')
        if payload.scope not in apps:
            raise HTTPException(403, detail='OWNER_SCOPE_NOT_ADMITTED')
        response = JSONResponse(_context(payload.scope), headers={'Cache-Control':'no-store'})
        response.set_cookie(COOKIE,payload.scope.value,httponly=True,samesite='strict')
        return response

    app.mount('/', ScopedConsole(apps, default_scope))
    return app


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Isolated existing-Console scope preview; never Owner8000')
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--port',type=int,default=8792)
    args = parser.parse_args()
    if args.port == 8000:
        raise SystemExit('OWNER_PORT_8000_REFUSED')
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1',args.port)) == 0:
            raise SystemExit('PREVIEW_PORT_OCCUPIED')
    # Defense in depth even when an operator launches directly from an Agent shell.
    root = checked_root(args.root)
    policy = {'policy':'LOOPBACK_ONLY','external_attempts':0}
    policy_path = root / 'NETWORK_POLICY.json'
    policy_path.write_text(json.dumps(policy),encoding='utf-8')
    def audit(event, values):
        host = None
        if event == 'socket.connect' and isinstance(values[1], tuple):
            host = values[1][0]
        elif event == 'socket.getaddrinfo':
            host = values[0]
        if host is not None and host not in {'127.0.0.1','::1','localhost'}:
            policy['external_attempts'] += 1
            policy_path.write_text(json.dumps(policy),encoding='utf-8')
            raise PermissionError('PREVIEW_EXTERNAL_CONNECT_BLOCKED')
    import sys
    sys.addaudithook(audit)
    import uvicorn
    uvicorn.run(build_app(root),host='127.0.0.1',port=args.port,access_log=False)


if __name__ == '__main__':
    main()
