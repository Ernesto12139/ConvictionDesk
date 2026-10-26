"""Runtime model admission uses the sole Writer's existing shared reservation.

Keys alone never authorize sends. A closed, absent, expired or uncertain budget
fails before the provider transport. No startup balance/model request is made.
"""
from pathlib import Path
from decimal import Decimal
import json
from datetime import datetime, timedelta, timezone

import httpx

from .llm import LLMError


class RuntimeModelBudget:
    def __init__(self, environment, provider):
        self.environment = dict(environment)
        self.provider = provider
        self.current = None

    def reserve(self, kind, model, amount, request_hash):
        from .experiment_budget import SharedExperimentBudget

        names = ('MIA_RUNTIME_BUDGET_LEDGER', 'MIA_RUNTIME_RESERVATION_ID', 'MIA_RUNTIME_BUDGET_DIR')
        values = [self.environment.get(name, '').strip() for name in names]
        if not all(values):
            raise LLMError('api_budget_authorization', 'Runtime model sends require an explicit shared reservation')
        ledger, reservation, directory = values
        if not Path(ledger).is_absolute() or not Path(directory).is_absolute():
            raise LLMError('api_budget_configuration', 'Runtime budget paths must be absolute')
        try:
            data = json.loads(Path(ledger).read_text(encoding='utf-8'))
            group = next(row for row in data['reservations'] if row['reservation_id'] == reservation)
            amounts = [Decimal(row['upper_usd']) for row in data['reservations']]
            ceiling = Decimal(data['ceiling_usd'])
            limits = [Decimal(value) for value in group['provider_limits_usd'].values()]
            if (not ceiling.is_finite() or not 0 < ceiling <= 10
                    or any(not value.is_finite() or value < 0 for value in [*amounts,*limits])
                    or sum(amounts) > ceiling or sum(limits) > Decimal(group['upper_usd'])
                    or type(group['max_requests']) is not int or not 1 <= group['max_requests'] <= 40):
                raise LLMError('api_budget_authorization', 'Runtime reservation limits or data rights are unqualified')
            from .research_egress import active_egress, active_egress_guard
            admission = active_egress.get()
            if active_egress_guard.get():
                active_egress_guard.get()()
            if admission is None:
                if (group.get('data_rights') != 'OWNED_SYNTHETIC'
                        or self.environment.get('MIA_RUNTIME_DATA_RIGHTS') != 'OWNED_SYNTHETIC'):
                    raise LLMError('data_rights_confirmation_required','A scoped local research confirmation is required')
            else:
                authorized = group.get('data_rights')
                authorized = ['SYNTHETIC'] if authorized == 'OWNED_SYNTHETIC' else authorized
                categories = {row['category'] for row in admission['sources']}
                if admission.get('owned_data', True):
                    categories.add('OWNED_PORTFOLIO')  # question and hypothesis are owned data
                target = {'OpenAI':'openai','DeepSeek':'deepseek','Compatible':'compatible'}[self.provider]
                if (not isinstance(authorized,list) or categories-set(authorized)
                        or admission['target'] != target or not admission.get('confirmed_at')):
                    raise LLMError('data_rights_reservation_scope','Reservation does not cover confirmed outbound categories')
            balance = group['provider_balance_checks'][self.provider]
            verified = datetime.fromisoformat(balance['verified_at'])
            expires = datetime.fromisoformat(balance['expires_at'])
            available = Decimal(balance['available'])
            now = datetime.now(timezone.utc)
            required = Decimal(group['provider_limits_usd'][self.provider])
            currency = balance['currency']
            if currency == 'CNY' and self.provider == 'DeepSeek':
                from .provider_pricing import RATE_CARD, qualified_rates
                qualified_rates(RATE_CARD)
                # Bound the same tokens using both published native price cards;
                # this is a token-price ratio, never an assumed exchange rate.
                required *= max(Decimal(RATE_CARD['deepseek_cny'][key]) /
                    Decimal(RATE_CARD['deepseek_usd'][key]) for key in ('input','output'))
            elif currency != 'USD':
                raise LLMError('api_budget_balance', 'Wallet currency has no qualified native token prices')
            if (not available.is_finite() or available < required
                    or verified.tzinfo is None or expires.tzinfo is None
                    or not verified <= now <= expires or expires-verified > timedelta(hours=1)):
                raise LLMError('api_budget_balance', 'A current sufficient native wallet check is required')
            self.current = SharedExperimentBudget(Path(directory) / (self.provider.lower()+'.sqlite3'),
                ledger=ledger, reservation_id=reservation, provider=self.provider)
            return self.current.reserve(kind, model, amount, request_hash)
        except LLMError:
            raise
        except (OSError, ValueError, KeyError, StopIteration, TypeError, ArithmeticError):
            raise LLMError('api_budget_authorization', 'Runtime reservation is unavailable or invalid') from None

    def record(self, request_id, status, usage=None):
        self.current.record(request_id, status, usage)


def runtime_transport_factory(environment, provider, *, inner_factory=None):
    """A fresh HTTP transport per request; admission remains durable across clients."""
    from .call_budget import BudgetedModelTransport
    from .provider_pricing import RATE_CARD

    environment = dict(environment)
    def create():
        return BudgetedModelTransport(
            inner_factory() if inner_factory else httpx.AsyncHTTPTransport(retries=0),
            RuntimeModelBudget(environment, provider), rate_card=RATE_CARD)
    return create
