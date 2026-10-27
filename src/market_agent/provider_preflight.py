"""Offline configuration rehearsal in the existing Console; never an Owner launcher.

Fixture configuration and fixture verification cannot establish live readiness.
Only the existing typed Hub/profiles are assembled. No actual vault is opened.
"""
import argparse
import json
from pathlib import Path

from fastapi.responses import JSONResponse

from .provider_vault import ProviderVault, VARIABLES
from .runtime_provider_hub import ProviderHub
from .unified_workbench import checked_root, _main_factory


def readiness(*, configured, authenticated=False, rights=False, budget=False):
    """Independent admission facts: a stored credential proves configuration only."""
    if not configured:
        return 'NOT_CONFIGURED'
    if not authenticated:
        return 'AUTH_NOT_VERIFIED'
    if not rights:
        return 'RIGHTS_BLOCKED'
    if not budget:
        return 'BUDGET_BLOCKED'
    return 'VERIFIED'


def controlled_environment(parent, *, hub_opt_in=False):
    # Keep Windows identity/profile discovery while excluding all provider,
    # endpoint, proxy, database and arbitrary Agent settings.
    keep = {'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'PATH', 'PATHEXT', 'COMSPEC',
            'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'OS'}
    environment = {k: v for k, v in parent.items() if k.upper() in keep}
    environment.update(MIA_RUNTIME_MODE='OWNER',
                       MIA_PROVIDER_HUB='1' if hub_opt_in else '0',
                       MIA_RESEARCH_STRATEGY='BASELINE_ONLY', MIA_JEV_MODE='OFF')
    return environment


def build_fixture_app(root, *, hub_opt_in=False):
    """Empty, marked fixture database; actual dispatch and credential writes denied."""
    root = checked_root(root)
    factory = _main_factory(root)
    from .agent import MockLLMClient
    from .model_profiles import ModelProfiles
    from .persistence import Store

    environment = controlled_environment({}, hub_opt_in=hub_opt_in)
    vault = ProviderVault({})
    # Every provider resolved in memory; even missing fixture entries must not
    # fall through to the real Windows DPAPI bridge.
    vault.overrides = {provider: '' for provider in VARIABLES}
    vault.overrides.update(OpenAI='offline-fixture', DeepSeek='offline-fixture')
    profiles = ModelProfiles(environment, multi_provider=True, vault=vault,
                             store=Store(root / 'provider-fixture.sqlite3'))
    hub = ProviderHub(environment, vault=vault, profiles=profiles)
    app = factory(root / 'provider-fixture.sqlite3', environment=environment,
                  llm_client=MockLLMClient(), provider_hub=hub, model_profiles=profiles)

    @app.middleware('http')
    async def fixture_boundary(request, call_next):
        if (request.url.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or request.client.host not in {'127.0.0.1', '::1', 'testclient'}):
            return JSONResponse({'detail': 'LOCAL_PREVIEW_ONLY'}, status_code=403)
        # Existing Hub same-origin/CSRF policy still governs preference writes.
        # Credentials, probes, research and all other mutations are refused.
        if (request.method not in {'GET', 'HEAD', 'OPTIONS'}
                and not (hub_opt_in and request.method == 'PUT'
                         and request.url.path == '/api/providers/preferences')):
            return JSONResponse({'detail': 'FIXTURE_DISPATCH_BLOCKED'}, status_code=409)
        return await call_next(request)

    @app.get('/api/configuration-preview')
    def configuration():
        return {'scope': 'OFFLINE_CONFIGURATION_FIXTURE', 'hub_enabled': hub_opt_in,
                'live_provider_verification': 'NOT_VERIFIED', 'paid_dispatch': 'BLOCKED',
                'real_owner_data': False, 'real_vault_opened': False,
                'providers': [{'provider': name, 'configured': bool(vault.overrides[name]),
                               'stage': readiness(configured=bool(vault.overrides[name])),
                               'provenance': 'FIXTURE_ONLY'} for name in VARIABLES]}

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8793)
    parser.add_argument('--enable-fixture-hub', action='store_true')
    args = parser.parse_args()
    if args.port == 8000 or not 1024 <= args.port <= 65535:
        raise SystemExit('OWNER_OR_INVALID_PORT_REFUSED')
    # Install the loopback-only audit guard before app/provider construction.
    import sys
    policy = {'external_attempts': 0, 'policy': 'LOOPBACK_ONLY'}
    root = checked_root(args.root)
    policy_path = root / 'CONFIGURATION_NETWORK_POLICY.json'
    policy_path.write_text(json.dumps(policy), encoding='utf-8')

    def audit(event, values):
        host = None
        if event == 'socket.connect' and isinstance(values[1], tuple):
            host = values[1][0]
        elif event == 'socket.getaddrinfo':
            host = values[0]
        if host is not None and host not in {'127.0.0.1', 'localhost', '::1'}:
            policy['external_attempts'] += 1
            policy_path.write_text(json.dumps(policy), encoding='utf-8')
            raise PermissionError('FIXTURE_EXTERNAL_CONNECT_BLOCKED')

    sys.addaudithook(audit)
    import uvicorn
    uvicorn.run(build_fixture_app(root, hub_opt_in=args.enable_fixture_hub),
                host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
