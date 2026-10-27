"""Explicit local startup configuration and conservative source-health readback.

No network calls, credential writes or database access. Returned environments
contain secrets and must only be passed in memory to the controlled child.
"""
from datetime import datetime, timezone

from .provider_preflight import controlled_environment
from .provider_vault import VARIABLES


def recovery_environment(parent, vault):
    environment = controlled_environment(parent, hub_opt_in=True)
    # Source constructors (notably Finnhub MarketPulse) still consume env keys.
    # Hydrate each independently; one vendor never substitutes for another.
    for provider, variable in VARIABLES.items():
        value = vault.get(provider)
        if value:
            environment[variable] = value
    # Do not inherit experiment reservations, proxies, endpoints or adaptive
    # routing. Native model transports retain their existing budget admission.
    return environment


def source_health(environment, hub=None, *, saved=None, now=None):
    now = now or datetime.now(timezone.utc)
    rows = []
    for provider, variable in VARIABLES.items():
        visible = bool(environment.get(variable) or hub is not None and hub.environment.get(variable))
        source_blocked = environment.get('MIA_SOURCE_POLICY_' + provider.upper()) == 'RIGHTS_BLOCKED'
        configured = visible
        error = None
        if hub is not None:
            configured, error = hub.vault.configured(provider)
        states = []
        if saved and saved.get(provider):
            states.append('SAVED')
        if visible:
            states.append('PROCESS_VISIBLE')
        elif configured:
            states.append('VAULT_VISIBLE')
        if not configured:
            states.append('NOT_CONFIGURED')
        if source_blocked and configured:
            states.append('RIGHTS_BLOCKED')
        elif configured and provider in {'Finnhub', 'Massive', 'SEC', 'Longbridge'} and not visible:
            states.append('RESTART_REQUIRED')
        check = hub.verifications.get(provider, {}) if hub else {}
        authenticated = False
        try:
            checked = datetime.fromisoformat(check['checked_at'])
            authenticated = (checked.tzinfo is not None
                and 0 <= (now - checked).total_seconds() <= 3600
                and 'AUTH_VERIFIED' in check.get('flags', [])
                and not check.get('error') and configured)
        except (KeyError, TypeError, ValueError):
            pass
        if authenticated:
            states.append('AUTH_VERIFIED')
        if provider in {'OpenAI', 'DeepSeek', 'Compatible', 'Jev'}:
            states.append('BUDGET_BLOCKED')
        rows.append({'provider': provider, 'states': states,
            'configured': configured, 'process_visible': visible,
            'source_client_enabled': bool(environment.get(variable)) and not source_blocked,
            'auth': 'AUTH_VERIFIED' if authenticated else 'NOT_VERIFIED',
            'rights': 'NOT_VERIFIED', 'freshness': 'NOT_VERIFIED',
            'error': 'VAULT_UNAVAILABLE' if error else None})
    return {'observed_at': now.isoformat(), 'providers': rows,
        'model_dispatch': 'REQUIRES_EXISTING_SCOPED_CONSENT_RIGHTS_AND_BUDGET_ADMISSION',
        'health_probe_requests': 0, 'actual_usable_model': None,
        'jev': 'JEV_NOT_VERIFIED', 'source_freshness': 'NOT_VERIFIED'}


def register_source_health(app, environment, hub):
    @app.get('/api/provider-source-health')
    def health():
        return source_health(environment, hub)


def profile_dispatch_blockers(environment, hub, profiles):
    """Preview readiness only; consent/rights are rechecked per actual send."""
    health = {row['provider']: row for row in source_health(environment, hub)['providers']}
    providers = {'openai': 'OpenAI', 'deepseek': 'DeepSeek', 'compatible': 'Compatible'}
    blockers = {}
    for profile in profiles:
        name = providers.get(profile['provider'])
        reason = None
        if not profile['available']:
            reason = 'MODEL_NOT_CONFIGURED'
        elif name is None or health[name]['auth'] != 'AUTH_VERIFIED':
            reason = 'AUTH_NOT_VERIFIED'
        elif not all(environment.get(k) for k in (
                'MIA_RUNTIME_BUDGET_LEDGER', 'MIA_RUNTIME_RESERVATION_ID', 'MIA_RUNTIME_BUDGET_DIR')):
            reason = 'BUDGET_RESERVATION_REQUIRED'
        else:
            from .provider_pricing import RATE_CARD, qualified_rates
            from .llm import LLMError
            try:
                qualified_rates(RATE_CARD)
            except LLMError:
                reason = 'PRICE_CARD_EXPIRED'
        blockers[profile['profile_id']] = reason
    return blockers
