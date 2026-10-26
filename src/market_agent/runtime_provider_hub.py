"""Small local provider control plane; configuration is not external admission."""
from datetime import datetime, timezone
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .llm import LLMError, StructuredLLMRequest
from .provider_vault import ProviderVault, VARIABLES
from .tdb import TDBError


CAPABILITIES = {'OpenAI':['structured_research','FAST','DEEP'],
    'DeepSeek':['structured_research','FAST','DEEP'], 'Compatible':['structured_research','SERVER_ENDPOINT_ONLY'],
    'Jev':['closed_choice','intent','tool_route','action_candidate'], 'Massive':['completed_close'],
    'Finnhub':['news','quote'], 'Longbridge':['personal_reference'], 'SEC':['filings','financials']}


class CredentialInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    secret: SecretStr = Field(min_length=1, max_length=4096)


class VerifyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['AUTH','MODEL'] = 'AUTH'
    allow_paid: bool = False


class PreferencesInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    fast: str = Field(max_length=64)
    deep: str = Field(max_length=64)


class ProbeAnswer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ok: bool


class ProviderHub:
    def __init__(self, environment, *, vault=None, profiles=None, http_factory=None):
        self.environment = dict(environment)
        self.vault = vault or ProviderVault(environment)
        self.profiles, self.http_factory = profiles, http_factory
        self.verifications = {}

    def require(self, provider):
        if provider not in VARIABLES:
            raise TDBError('PROVIDER_UNKNOWN', 404)

    def describe(self, provider):
        self.require(provider)
        configured, error = self.vault.configured(provider)
        check = self.verifications.get(provider, {})
        # Verification is bound to the in-memory credential and expires after 1h.
        checked = check.get('checked_at')
        if checked and (datetime.now(timezone.utc)-datetime.fromisoformat(checked)).total_seconds() > 3600:
            check = {}
        flags = (['CONFIGURED_LOCAL'] if configured else []) + check.get('flags',[])
        if error or check.get('error'):
            flags.append('ERROR')
        return {'provider':provider,'capabilities':CAPABILITIES[provider],
            'credential':'********' if configured else None,
            'status':flags[-1] if flags else 'NOT_CONFIGURED',
            'states':[*flags,'BALANCE_UNKNOWN'] if not check.get('balance_verified') else flags,
            'checked_at':check.get('checked_at'), 'error':error or check.get('error'),
            'verification':{'AUTH':{'may_bill':provider in {'Massive','Finnhub','Longbridge'}},
                'MODEL':{'may_bill':True}},
            'storage':'WINDOWS_DPAPI_OR_ENVIRONMENT' if provider != 'SEC' else 'PROCESS_CONTACT_IDENTIFIER',
            'activation':'LIVE_FOR_MODEL_PROFILES' if provider in {'OpenAI','DeepSeek','Compatible','Jev'} else 'SOURCE_RESTART_REQUIRED',
            'balance_available':check.get('balance_available'),
            'actual_model':check.get('actual_model'), 'usage':check.get('usage',{})}

    def change(self, provider, secret=None):
        self.require(provider)
        if secret is None:
            self.vault.remove(provider)
        else:
            self.vault.set(provider,secret)
        self.verifications.pop(provider,None)
        return self.describe(provider)

    async def verify(self, provider, body):
        self.require(provider)
        secret = self.vault.get(provider)
        if not secret:
            raise TDBError('PROVIDER_NOT_CONFIGURED', 409)
        stamp = {'checked_at':datetime.now(timezone.utc).isoformat(),'flags':[]}
        self.verifications[provider] = stamp
        try:
            if body.kind == 'MODEL':
                if not body.allow_paid:
                    raise TDBError('PAID_VERIFICATION_CONFIRMATION_REQUIRED',409)
                profile_id = {'OpenAI':'sol-fast','DeepSeek':'deepseek-fast','Compatible':'compatible-fast'}.get(provider)
                if profile_id is None or self.profiles is None:
                    raise TDBError('MODEL_PROBE_NOT_SUPPORTED',409)
                profile = self.profiles.resolve(profile_id)
                native = self.profiles.native_client(profile)
                from .research_egress import active_egress, active_egress_guard
                from .tdb import content_hash
                def health_guard():
                    if self.vault.get(provider) != secret:
                        raise LLMError("egress_confirmation_required","Provider credential changed during probe")
                    return {"health_probe":True}
                guard_token = active_egress_guard.set(health_guard)
                token = active_egress.set({'target':profile.provider,'confirmed_at':stamp['checked_at'],
                    'owned_data':False,'sources':[{'category':'SYNTHETIC'}],'purpose':'provider.health',
                    'model':profile.model,'destination':{'openai':'https://api.openai.com/v1/responses',
                    'deepseek':'https://api.deepseek.com/chat/completions','compatible':self.environment.get('LLM_BASE_URL','').rstrip('/')+'/chat/completions'}[profile.provider],
                    'payload_hash':content_hash({'synthetic':True,'required_ok':True})})
                try:
                    response = await native.complete_structured(StructuredLLMRequest(purpose='provider.health',
                        response_model=ProbeAnswer,facts={'synthetic':True,'required_ok':True},evidence_ids=[],
                        numeric_fact_tokens=[],generation_mode='generate',max_output_tokens=256,
                        system_instruction='Return JSON {"ok":true}. This is synthetic provider verification.'))
                finally:
                    active_egress.reset(token)
                    active_egress_guard.reset(guard_token)
                if not response.ok:
                    raise LLMError('schema_validation','Health reply did not satisfy probe')
                stamp.update(flags=['AUTH_VERIFIED','ENTITLEMENT_VERIFIED','MODEL_READY'],
                    actual_model=getattr(native,'call_metadata',lambda:{})().get('actual_model',native.model_name),
                    usage=getattr(native,'call_metadata',lambda:{})().get('usage',{}))
            else:
                if provider in {'Massive','Finnhub','Longbridge'} and not body.allow_paid:
                    raise TDBError('POTENTIALLY_BILLED_VERIFICATION_CONFIRMATION_REQUIRED',409)
                endpoints = {'OpenAI':'https://api.openai.com/v1/models',
                    'DeepSeek':'https://api.deepseek.com/user/balance'}
                # Public Jev models and SEC endpoints do not prove authentication;
                # do not manufacture an AUTH_VERIFIED result from public HTTP 200.
                if provider not in endpoints:
                    raise TDBError('AUTH_PROBE_NOT_SUPPORTED',409)
                factory = self.http_factory or (lambda:httpx.AsyncClient(trust_env=False,follow_redirects=False))
                async with factory() as client:
                    response = await client.get(endpoints[provider],headers={'Authorization':'Bearer '+secret},timeout=8)
                if response.status_code != 200:
                    raise LLMError('authentication' if response.status_code in {401,403} else 'provider_unavailable','Probe rejected')
                value = response.json()
                valid = (isinstance(value.get('data'),list) if provider == 'OpenAI'
                    else type(value.get('is_available')) is bool and isinstance(value.get('balance_infos'),list))
                if not valid:
                    raise LLMError('schema_validation','Probe schema invalid')
                stamp['flags'] = ['AUTH_VERIFIED']
                # Wallet check does not establish model entitlement or free credit.
                stamp['balance_verified'] = provider == 'DeepSeek'
                if provider == 'DeepSeek':
                    stamp['balance_available'] = value['is_available']
        except TDBError:
            raise
        except httpx.TimeoutException:
            stamp['error'] = 'PROVIDER_TIMEOUT'
        except LLMError as exc:
            from .runtime_errors import failure_kind
            stamp['error'] = failure_kind(exc.code)
        except (httpx.RequestError,ValueError,TypeError,AttributeError):
            stamp['error'] = 'PROVIDER_UNAVAILABLE'
        return self.describe(provider)


def register_provider_hub(app, hub, token):
    @app.get('/api/providers/session')
    def session():
        return {'csrf_token':token}

    @app.get('/api/providers')
    def inventory():
        return {'providers':[hub.describe(provider) for provider in VARIABLES]}

    @app.put('/api/providers/{provider}/credential')
    def set_credential(provider: str, body: CredentialInput):
        return hub.change(provider,body.secret.get_secret_value())

    @app.delete('/api/providers/{provider}/credential')
    def remove_credential(provider: str):
        return hub.change(provider)

    @app.post('/api/providers/{provider}/verify')
    async def verify(provider: str, body: VerifyInput):
        return await hub.verify(provider,body)

    @app.get('/api/providers/preferences')
    def preferences():
        return {mode.lower():profile for mode,profile in hub.profiles.defaults.items()}

    @app.put('/api/providers/preferences')
    def set_preferences(body: PreferencesInput):
        hub.profiles.set_defaults(body.fast,body.deep)
        return preferences()
