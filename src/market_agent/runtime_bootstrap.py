"""Optional backend assembly; the Product Lead owns main.py and frontend registration.

No import-time app, database or credential access. The standalone factory reuses
the product's existing provider construction, then exposes only the protected
research/control API. It does not replace the product Console or Today app.
"""
from dataclasses import dataclass
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .jev_runtime import JevRuntime, register_jev_policy
from .local_api_security import install_local_boundary
from .model_profiles import ModelProfiles
from .research_api import register_research
from .research_egress import ResearchEgress, register_egress
from .runtime_provider_hub import ProviderHub, register_provider_hub
from .tdb import TDBError


@dataclass
class RuntimeComponents:
    profiles: ModelProfiles
    egress: ResearchEgress
    jev: JevRuntime
    hub: ProviderHub

    def research_options(self):
        return {'model_profiles': self.profiles, 'egress': self.egress, 'jev_runtime': self.jev}

    def register_after_research(self, app):
        register_egress(app, app.state.research_service, self.egress)


def configure_runtime(app, agent, environment, *, provider_hub=None, model_profiles=None):
    """Lead seam: call before register_research; replace legacy Hub registration.

    Reject duplicate assembly rather than silently choosing an endpoint by order.
    DPAPI is Windows-user storage, not isolation from other same-user processes.
    """
    if environment.get('MIA_PROVIDER_HUB') != '1' or environment.get('MIA_RUNTIME_MODE') == 'PUBLIC_DEMO':
        raise ValueError('RUNTIME_REQUIRES_EXPLICIT_LOCAL_OWNER_OPT_IN')
    if any(getattr(route, 'path', '').startswith(('/api/providers', '/api/research/tasks'))
           for route in app.routes):
        raise ValueError('RUNTIME_REGISTRATION_CONFLICT_LEAD_MUST_SELECT_ONE_HUB_AND_RESEARCH_ROUTER')
    token = install_local_boundary(app, enabled=True)
    hub = provider_hub or ProviderHub(environment)
    profiles = model_profiles or ModelProfiles(environment, multi_provider=True, vault=hub.vault, store=agent.store)
    hub.profiles = profiles
    egress = ResearchEgress(agent.store, profiles, environment)
    jev = JevRuntime.from_environment(environment, hub.vault, agent.store)
    app.state.provider_hub = hub
    register_provider_hub(app, hub, token)
    register_jev_policy(app, jev)
    return RuntimeComponents(profiles, egress, jev, hub)


def create_runtime_app(agent, environment, *, provider_hub=None, model_profiles=None):
    app = FastAPI(title='ConvictionDesk Research Runtime', version='0.9.0')
    components = configure_runtime(app, agent, environment, provider_hub=provider_hub, model_profiles=model_profiles)
    register_research(app, agent, **components.research_options(),
        strategy_policy=environment.get('MIA_RESEARCH_STRATEGY', 'BASELINE_ONLY'),
        answer_contract=environment.get('MIA_RESEARCH_CONTRACT', 'GROUNDED_MEMO'))
    components.register_after_research(app)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({'detail': 'INVALID_REQUEST'}, status_code=422)

    @app.exception_handler(TDBError)
    async def decision_failure(request, exc):
        return JSONResponse({'detail': {'code': str(exc), 'run_id': None}}, status_code=exc.status_code)

    @app.get('/health')
    def health():
        return {'status': 'ok', 'scope': 'ISOLATED_RESEARCH_BACKEND', 'owner_acceptance': 'NOT_ASSESSED'}

    return app


def create_app(*, provider_hub=None, model_profiles=None, environment=None, **product_options):
    """Inject existing product providers for deterministic integration or local use."""
    from .main import create_app as product_app
    environment = dict(os.environ if environment is None else environment)
    product = product_app(environment=environment, **product_options)
    return create_runtime_app(product.state.research_service.agent, environment,
        provider_hub=provider_hub, model_profiles=model_profiles)


def app_from_environment():
    """uvicorn --factory entry; never defaults to an Owner database."""
    environment = dict(os.environ)
    isolated = environment.get('MIA_ISOLATED_ROOT')
    database = environment.get('MIA_DB_PATH')
    if not isolated or not database:
        raise ValueError('RUNTIME_REQUIRES_EXPLICIT_ISOLATED_ROOT_AND_DATABASE')
    Path(database).resolve().relative_to(Path(isolated).resolve())
    environment.setdefault('MIA_PROVIDER_HUB', '1')
    from .main import app_from_environment as product_app
    product = product_app(environment)
    return create_runtime_app(product.state.research_service.agent, environment)
