"""Server-owned model profiles. API keys remain in the local environment."""
from dataclasses import dataclass
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
    def __init__(self, environment, *, profiles=None, client_factory=None):
        self.environment = dict(environment)
        entries = profiles or [ModelProfile('sol-fast','FAST',max_output_tokens=3072,timeout_seconds=45),
                               ModelProfile('sol-deep','DEEP')]
        self.profiles = {profile.profile_id:profile for profile in entries}
        if len(self.profiles) != len(entries):
            raise ValueError('Duplicate model profile')
        for profile in entries:
            if (profile.provider!='openai' or profile.model!='gpt-6.1-sol' or profile.mode not in {'FAST','DEEP'}
                    or not 256 <= profile.max_output_tokens <= (3072 if profile.mode=='FAST' else 8192)
                    or not 0 < profile.timeout_seconds <= (45 if profile.mode=='FAST' else 90)):
                raise ValueError('Profile exceeds the verified adapter/mode contract')
            if profile.fallback_id:
                fallback = self.profiles.get(profile.fallback_id)
                if not fallback or fallback.mode!=profile.mode or fallback.fallback_id:
                    raise ValueError('Fallback must be one bounded compatible profile')
        self.client_factory = client_factory

    def resolve(self, profile_id=None, mode=None):
        profile_id = profile_id or ('sol-fast' if mode=='FAST' else 'sol-deep')
        profile = self.profiles.get(profile_id)
        if not profile or mode and profile.mode != mode:
            raise TDBError('RESEARCH_MODEL_PROFILE_INVALID',422)
        if not self.environment.get(profile.key_env):
            raise TDBError('RESEARCH_MODEL_PROFILE_UNAVAILABLE',422)
        return profile

    def describe(self):
        return [{'profile_id':p.profile_id,'provider':p.provider,'model':p.model,'mode':p.mode,
            'available':bool(self.environment.get(p.key_env)), 'key_source':'SERVER_ENVIRONMENT',
            'max_output_tokens':p.max_output_tokens,'max_supplemental_tools':int(p.mode=='DEEP'),
            'timeout_seconds':p.timeout_seconds,'fallback_profile':p.fallback_id}
            for p in self.profiles.values()]

    def native_client(self, profile):
        if self.client_factory:
            return self.client_factory(profile)
        return OpenAIResponsesLLMClient(self.environment[profile.key_env],model=profile.model,
            timeout=profile.timeout_seconds,max_transport_attempts=1)

    def client(self, profile_id):
        profile = self.resolve(profile_id)
        registry = self

        class ProfileClient:
            provider = profile.provider
            model_name = profile.model

            def __init__(self):
                self.metadata = {}

            async def complete_structured(self, request):
                attempts = []
                for candidate in [profile,*([registry.profiles[profile.fallback_id]] if profile.fallback_id else [])]:
                    native = registry.native_client(candidate)
                    bounded = request.model_copy(update={'max_output_tokens':min(request.max_output_tokens,candidate.max_output_tokens)})
                    try:
                        result = await native.complete_structured(bounded)
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
