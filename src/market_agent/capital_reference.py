"""Personal display of saved provider fields; never a research/model input.

No retrieval, persistence, arithmetic or capital-action all-clear occurs here.
The server must explicitly enable personal display and select a private capture
directory. A returned field is SOURCE_REPORTED_REFERENCE, not a computed value.
"""
import hashlib
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .domain import utc
from .valuation import number


class LocalCapitalReference:
    def __init__(self, directory):
        self.directory = Path(directory)

    @classmethod
    def from_environment(cls, environment):
        if (environment.get('MIA_CAPITAL_REFERENCE_MODE') == 'personal-display'
                and environment.get('MIA_CAPITAL_REFERENCE_DIR')):
            return cls(environment['MIA_CAPITAL_REFERENCE_DIR'])
        return None

    def _json(self, filename):
        path = self.directory / filename
        if path.stat().st_size > 2_000_000:
            raise ValueError('capture too large')
        raw = path.read_bytes()
        if len(raw) > 2_000_000:
            raise ValueError('capture too large')
        return raw, json.loads(raw, parse_float=Decimal)

    def read(self, symbol, cutoff):
        """Recheck raw hashes on every read and fail closed without path disclosure.

        Retrieval proves observation only. The provider does not supply the cap's
        effective timestamp; the separately captured close must not fill it in.
        Nor may today's capture be attached to an older research cutoff.
        """
        empty = {'status': 'UNAVAILABLE', 'symbol': symbol,
                 'reason': 'NO_QUALIFIED_SAVED_PROVIDER_REFERENCE'}
        if not symbol or not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,14}', symbol):
            return empty
        try:
            _, journal = self._json('journal.json')
            records = journal['records']
            if not isinstance(records, list) or len(records) > 100:
                return empty
            inputs = {}
            for kind in ('overview', 'close'):
                matching = [r for r in records if r['symbol'] == symbol and r['endpoint'] == kind]
                if len(matching) != 1:
                    return empty
                record = matching[0]
                raw, payload = self._json(f'{symbol}-{kind}.json')
                if (record['status'] != 200 or payload['status'] != 'OK'
                        or hashlib.sha256(raw).hexdigest() != record['sha256']):
                    return empty
                observed = utc(datetime.fromisoformat(record['retrieved_at']))
                if observed > cutoff:
                    return {**empty, 'reason': 'NOT_OBSERVED_AT_RESEARCH_CUTOFF'}
                if cutoff-observed > timedelta(days=7):
                    return {'status':'STALE','symbol':symbol,
                        'reason':'SAVED_PROVIDER_REFERENCE_OLDER_THAN_SEVEN_DAYS',
                        'observed_at':observed.isoformat()}
                inputs[kind] = (record, payload, observed)
            overview_record, overview, observed = inputs['overview']
            close_record, close, close_observed = inputs['close']
            detail = overview['results']
            # Exact endpoint identities are part of the capture contract. No
            # credentials or arbitrary response URL can be reflected into the UI.
            overview_uri = f'https://api.massive.com/v3/reference/tickers/{symbol}'
            session_date = date.fromisoformat(close['from'])
            close_uri = f'https://api.massive.com/v1/open-close/{symbol}/{session_date}'
            if (overview_record['url'] != overview_uri or close_record['url'] != close_uri
                    or overview_record.get('params') != {}
                    or close_record.get('params') != {'adjusted': 'false'}
                    or detail['ticker'] != symbol or close['symbol'] != symbol
                    or detail['currency_name'].upper() != 'USD' or detail['market'] != 'stocks'
                    or detail.get('locale') != 'us' or detail.get('active') is not True
                    or not detail.get('cik') or not detail.get('share_class_figi')
                    or session_date > min(close_observed.date(), cutoff.date())
                    or session_date.weekday() >= 5):
                return empty
            cap, price = number(detail.get('market_cap')), number(close.get('close'))
            if cap is None or cap <= 0 or price is None or price <= 0:
                return empty
            cap_id = 'local:sha256:' + overview_record['sha256']
            close_id = 'local:sha256:' + close_record['sha256']
            return {
                'status': 'AVAILABLE', 'symbol': symbol,
                'qualification_status': 'SOURCE_REPORTED_REFERENCE',
                'what': 'PROVIDER_REPORTED_MARKET_CAP', 'value': str(cap),
                'unit': 'USD', 'currency': 'USD', 'provider': 'massive',
                'raw_field': 'results.market_cap', 'source_uri': overview_uri,
                'source_id': cap_id, 'source_sha256': overview_record['sha256'],
                'observed_at': observed.isoformat(), 'provider_as_of': None,
                'price_as_of': None, 'shares_as_of': None,
                'basis': 'PROVIDER_LATEST_CLOSE_TIMES_WEIGHTED_EQUIVALENT_SHARES',
                'computation': 'PROVIDER_REPORTED_NOT_INDEPENDENTLY_COMPUTED',
                'issuer_cik': detail['cik'], 'share_class_figi': detail['share_class_figi'],
                'use_scope': 'LOCAL_PERSONAL_DISPLAY_ONLY',
                'context_binding': 'OBSERVED_BY_CUTOFF_NOT_PART_OF_IMMUTABLE_TASK',
                'external_model_transfer': False, 'derived_multiples': False,
                'regular_close_reference': {
                    'value': str(price), 'unit': 'USD/shares', 'currency': 'USD',
                    'price_as_of': session_date.isoformat(),
                    'time_precision': 'TRADING_DATE', 'session': 'COMPLETED_CLOSE',
                    'price_basis': 'unadjusted', 'raw_field': 'close',
                    'observed_at': close_observed.isoformat(), 'source_uri': close_uri,
                    'source_id': close_id, 'source_sha256': close_record['sha256'],
                    'is_proven_cap_price_input': False,
                },
                'limitations': [
                    'OBSERVATION_TIME_IS_NOT_PROVIDER_EFFECTIVE_TIME',
                    'CAP_PRICE_AND_SHARE_EFFECTIVE_DATES_UNKNOWN',
                    'SEPARATE_CLOSE_NOT_PROVEN_TO_BE_CAP_INPUT',
                    'WEIGHTED_EQUIVALENT_SHARES_MAY_SPAN_MULTIPLE_CLASSES',
                    'ISSUANCE_BUYBACK_CONVERSION_AND_SPLIT_ALIGNMENT_NOT_VERIFIED',
                    'NOT_REALTIME_NOT_FAIR_VALUE_NOT_STANDARD_PE',
                    'NO_DERIVED_CALCULATION_MODEL_TRANSFER_OR_REDISTRIBUTION_RIGHTS_INFERRED',
                ],
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            return empty
