"""Server-owned model profiles. API keys remain in the local environment."""
from dataclasses import dataclass
import asyncio
import json

from .llm import LLMError, OpenAIResponsesLLMClient
from .tdb import TDBError


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    mode: str
    model: str = 'gpt-6.1-sol'
    provider: str = 'openai'
    key_env: str = 'OPENAI_API_KEY'
    max_output_tokens: int = 8192
    timeout_seconds: int = 90
    fallback_id: str | None = None


class ModelProfiles:
    def __init__(self, environment, *, profiles=None, client_factory=None, multi_provider=False, vault=None, store=None):
        self.environment = dict(environment)
        self.vault, self.store = vault, store
        self.defaults = {'FAST':'sol-fast','DEEP':'sol-deep'}
        entries = profiles or [ModelProfile('sol-fast','FAST',max_output_tokens=3072,timeout_seconds=45),
                               ModelProfile('sol-deep','DEEP')]
        if profiles is None and multi_provider:
            entries += [ModelProfile('deepseek-fast','FAST',model='deepseek-flash',provider='deepseek',
                key_env='DEEPSEEK_API_KEY',max_output_tokens=3072,timeout_seconds=45),
                ModelProfile('deepseek-deep','DEEP',model='deepseek-flash',provider='deepseek',key_env='DEEPSEEK_API_KEY')]
            if environment.get('LLM_BASE_URL') and environment.get('LLM_MODEL'):
                from urllib.parse import urlsplit
                endpoint = urlsplit(environment['LLM_BASE_URL'])
                if endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                    raise ValueError('Compatible endpoint must be server-owned HTTPS')
                entries += [ModelProfile('compatible-'+mode.lower(), mode, model=environment['LLM_MODEL'],
                    provider='compatible',key_env='LLM_API_KEY', max_output_tokens=3072 if mode=='FAST' else 8192,
                    timeout_seconds=45 if mode=='FAST' else 90) for mode in ('FAST','DEEP')]
        self.profiles = {profile.profile_id:profile for profile in entries}
        if len(self.profiles) != len(entries):
            raise ValueError('Duplicate model profile')
        for profile in entries:
            adapter_allowed = (profile.provider == 'openai' and profile.model == 'gpt-6.1-sol'
                or multi_provider and profile.provider == 'deepseek' and profile.model == 'deepseek-flash'
                or multi_provider and profile.provider == 'compatible' and profile.model == environment.get('LLM_MODEL'))
            if (not adapter_allowed or profile.mode not in {'FAST','DEEP'}
                    or not 256 <= profile.max_output_tokens <= (3072 if profile.mode=='FAST' else 8192)
                    or not 0 < profile.timeout_seconds <= (45 if profile.mode=='FAST' else 90)):
                raise ValueError('Profile exceeds the verified adapter/mode contract')
            if profile.fallback_id:
                fallback = self.profiles.get(profile.fallback_id)
                if not fallback or fallback.mode!=profile.mode or fallback.fallback_id:
                    raise ValueError('Fallback must be one bounded compatible profile')
        self.client_factory = client_factory
        if store is not None:
            with store._connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS provider_preferences (mode TEXT PRIMARY KEY, profile TEXT NOT NULL)')
                for mode, profile_id in db.execute('SELECT mode,profile FROM provider_preferences'):
                    if profile_id in self.profiles and self.profiles[profile_id].mode == mode:
                        self.defaults[mode] = profile_id

    def key(self, profile):
        if self.vault:
            return self.vault.get({'openai':'OpenAI','deepseek':'DeepSeek','compatible':'Compatible'}[profile.provider])
        return self.environment.get(profile.key_env, '')

    def set_defaults(self, fast, deep):
        choices = {mode:self.resolve(value,mode).profile_id for mode,value in [('FAST',fast),('DEEP',deep)]}
        if self.store is not None:
            with self.store._connect() as db:
                db.executemany('INSERT OR REPLACE INTO provider_preferences VALUES (?,?)', choices.items())
        self.defaults = choices

    def resolve(self, profile_id=None, mode=None):
        profile_id = profile_id or self.defaults[mode or 'DEEP']
        profile = self.profiles.get(profile_id)
        if not profile or mode and profile.mode != mode:
            raise TDBError('RESEARCH_MODEL_PROFILE_INVALID',422)
        if not self.key(profile):
            raise TDBError('RESEARCH_MODEL_PROFILE_UNAVAILABLE',422)
        return profile

    def describe(self):
        return [{'profile_id':p.profile_id,'provider':p.provider,'model':p.model,'mode':p.mode,
            'available':bool(self.key(p)), 'key_source':'LOCAL_VAULT_OR_ENVIRONMENT' if self.vault else 'SERVER_ENVIRONMENT',
            'max_output_tokens':p.max_output_tokens,'max_supplemental_tools':int(p.mode=='DEEP'),
            'timeout_seconds':p.timeout_seconds,'fallback_profile':p.fallback_id}
            for p in self.profiles.values()]

    def native_client(self, profile):
        if self.client_factory:
            return self.client_factory(profile)
        from .runtime_budget import runtime_transport_factory
        if profile.provider == 'deepseek':
            from .deepseek import DeepSeekLLMClient
            return DeepSeekLLMClient(self.key(profile), model=profile.model,timeout=profile.timeout_seconds,
                max_tokens=profile.max_output_tokens,transport_factory=runtime_transport_factory(self.environment,'DeepSeek'))
        if profile.provider == 'compatible':
            from .llm import OpenAICompatibleLLMClient
            return OpenAICompatibleLLMClient(self.environment['LLM_BASE_URL'],self.key(profile),model=profile.model,
                timeout=profile.timeout_seconds,transport_factory=runtime_transport_factory(self.environment,'Compatible'))
        return OpenAIResponsesLLMClient(self.key(profile),model=profile.model,
            timeout=profile.timeout_seconds,max_transport_attempts=1,
            transport_factory=runtime_transport_factory(self.environment, 'OpenAI'))

    def client(self, profile_id):
        profile = self.resolve(profile_id)
        registry = self

        class ProfileClient:
            provider = profile.provider
            model_name = profile.model

            def __init__(self):
                self.metadata = {}

            def reset_call_metadata(self):
                self.metadata = {}

            async def complete_structured(self, request):
                self.reset_call_metadata()
                attempts = []
                for candidate in [profile,*([registry.profiles[profile.fallback_id]] if profile.fallback_id else [])]:
                    native = registry.native_client(candidate)
                    bounded = request.model_copy(update={'max_output_tokens':min(request.max_output_tokens,candidate.max_output_tokens)})
                    try:
                        result = await native.complete_structured(bounded)
                    except asyncio.CancelledError:
                        attempts.append({'profile_id':candidate.profile_id,'status':'cancelled'})
                        self.metadata = {**getattr(native,'call_metadata',lambda:{})(),
                            'context_metrics':{'model_profile':profile.profile_id,'profile_attempts':json.dumps(attempts)}}
                        raise
                    except LLMError as exc:
                        attempts.append({'profile_id':candidate.profile_id,'status':'error','error':exc.code})
                        self.metadata = {**getattr(native,'call_metadata',lambda:{})(),
                            'context_metrics':{'model_profile':profile.profile_id,'profile_attempts':json.dumps(attempts)}}
                        if candidate != profile or not profile.fallback_id or exc.code not in {'timeout','transport','provider_unavailable','rate_limit'}:
                            raise
                    else:
                        attempts.append({'profile_id':candidate.profile_id,'status':'success'})
                        self.metadata = {**getattr(native,'call_metadata',lambda:{})(),
                            'actual_model':getattr(native,'call_metadata',lambda:{})().get('actual_model',native.model_name),
                            'context_metrics':{'model_profile':profile.profile_id,'profile_attempts':json.dumps(attempts)}}
                        return result

            def call_metadata(self):
                return self.metadata

        return ProfileClient()
