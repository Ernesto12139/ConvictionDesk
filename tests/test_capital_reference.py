"""Independent synthetic saved-field oracles; no market values or network."""
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json

from fastapi.testclient import TestClient
import pytest

from market_agent.capital_reference import LocalCapitalReference
from market_agent.main import create_app
from market_agent.tdb import content_hash
from test_valuation import NOW, task


def capture(folder, symbol='APP', *, cap='98765.432100', close='12.50', **changes):
    folder.mkdir(parents=True, exist_ok=True)
    overview = {'status': 'OK', 'results': {'ticker': symbol, 'market': 'stocks',
        'locale': 'us', 'active': True, 'currency_name': 'usd', 'cik': 'synthetic',
        'share_class_figi': 'synthetic-class', 'market_cap': cap,
        'weighted_shares_outstanding': '111', 'share_class_shares_outstanding': '99'}}
    daily = {'status': 'OK', 'symbol': symbol, 'from': '2026-10-16',
             'close': close, 'afterHours': '999.99', 'preMarket': '0.01'}
    for key, value in changes.items():
        overview['results'][key] = value
    records = []
    for kind, payload, url, params in [
        ('overview', overview, f'https://api.massive.com/v3/reference/tickers/{symbol}', {}),
        ('close', daily, f'https://api.massive.com/v1/open-close/{symbol}/2026-10-16', {'adjusted': 'false'}),
    ]:
        raw = json.dumps(payload).encode()
        (folder / f'{symbol}-{kind}.json').write_bytes(raw)
        records.append({'symbol': symbol, 'endpoint': kind, 'url': url, 'params': params,
                        'status': 200, 'sha256': sha256(raw).hexdigest(),
                        'retrieved_at': NOW.isoformat()})
    (folder / 'journal.json').write_text(json.dumps({'records': records}))
    return records


def test_reported_cap_is_not_price_times_shares_or_independent_computation(tmp_path):
    capture(tmp_path)
    row = LocalCapitalReference(tmp_path).read('APP', NOW)
    assert row['status'] == 'AVAILABLE'
    assert row['value'] == '98765.432100'  # Deliberately != close * either shares field.
    assert row['raw_field'] == 'results.market_cap'
    assert row['qualification_status'] == 'SOURCE_REPORTED_REFERENCE'
    assert row['computation'] == 'PROVIDER_REPORTED_NOT_INDEPENDENTLY_COMPUTED'
    assert row['provider_as_of'] is row['shares_as_of'] is row['price_as_of'] is None
    assert row['observed_at'] == NOW.isoformat()
    assert row['external_model_transfer'] is row['derived_multiples'] is False
    assert row['regular_close_reference']['value'] == '12.50'
    assert row['regular_close_reference']['price_as_of'] == '2026-10-16'
    assert row['regular_close_reference']['is_proven_cap_price_input'] is False
    assert all(key not in row for key in ('pe_ttm', 'price_sales_ttm', 'capital_basis_check'))


@pytest.mark.parametrize('symbol', ['APP', 'SMR', 'GOOG', 'BRK.B'])
def test_multi_class_and_loss_companies_can_display_a_reported_field_without_deriving_equity(tmp_path, symbol):
    capture(tmp_path, symbol)
    row = LocalCapitalReference(tmp_path).read(symbol, NOW)
    assert row['status'] == 'AVAILABLE'
    assert 'WEIGHTED_EQUIVALENT_SHARES_MAY_SPAN_MULTIPLE_CLASSES' in row['limitations']
    assert row['derived_multiples'] is False


@pytest.mark.parametrize('changes', [
    {'cap': 'NaN'}, {'cap': '-1'}, {'cap': '0'}, {'cap': True}, {'cap': None},
    {'currency_name': 'eur'}, {'market': 'crypto'}, {'locale': 'global'},
    {'active': False}, {'cik': ''}, {'share_class_figi': None}, {'ticker': 'OTHER'},
    {'close': 'Infinity'}, {'close': False},
])
def test_bad_fields_never_generate_a_value(tmp_path, changes):
    capture(tmp_path, **changes)
    row = LocalCapitalReference(tmp_path).read('APP', NOW)
    assert row['status'] == 'UNAVAILABLE' and 'value' not in row


def test_future_capture_cannot_become_pit_research_evidence(tmp_path):
    capture(tmp_path)
    row = LocalCapitalReference(tmp_path).read('APP', NOW - timedelta(seconds=1))
    assert row['reason'] == 'NOT_OBSERVED_AT_RESEARCH_CUTOFF' and 'value' not in row


@pytest.mark.parametrize('days',[8,60])
def test_aged_snapshot_cannot_look_like_a_current_available_reference(tmp_path,days):
    capture(tmp_path)
    row=LocalCapitalReference(tmp_path).read('APP',NOW+timedelta(days=days))
    assert row['status']=='STALE' and 'value' not in row
    assert row['reason']=='SAVED_PROVIDER_REFERENCE_OLDER_THAN_SEVEN_DAYS'
    assert LocalCapitalReference(tmp_path).read('APP',NOW+timedelta(days=7))['status']=='AVAILABLE'


def test_raw_tamper_and_journal_ambiguity_fail_closed(tmp_path):
    records = capture(tmp_path)
    path = tmp_path / 'APP-overview.json'
    path.write_bytes(path.read_bytes() + b' ')
    assert LocalCapitalReference(tmp_path).read('APP', NOW)['status'] == 'UNAVAILABLE'
    records = capture(tmp_path)
    records.append(deepcopy(records[0]))
    (tmp_path / 'journal.json').write_text(json.dumps({'records': records}))
    assert LocalCapitalReference(tmp_path).read('APP', NOW)['status'] == 'UNAVAILABLE'


@pytest.mark.parametrize('change', [
    lambda r: r[1]['params'].update(adjusted='true'),
    lambda r: r[0]['params'].update(date='2025-01-01'),
    lambda r: r[0].update(url='https://evil.example/token'),
    lambda r: r[0].update(retrieved_at='2026-10-17T12:00:00'),
])
def test_adjusted_prices_backfilled_overviews_and_unknown_timezones_are_rejected(tmp_path, change):
    records = capture(tmp_path)
    change(records)
    (tmp_path / 'journal.json').write_text(json.dumps({'records': records}))
    assert LocalCapitalReference(tmp_path).read('APP', NOW)['status'] == 'UNAVAILABLE'


def test_empty_split_list_and_adjustment_ratio_cannot_grant_calculation(tmp_path):
    capture(tmp_path)
    (tmp_path / 'APP-splits.json').write_text('{"status":"OK","results":[]}')
    (tmp_path / 'adjusted-ratio.json').write_text('{"ratio":"1"}')
    row = LocalCapitalReference(tmp_path).read('APP', NOW)
    assert row['derived_multiples'] is False
    assert 'ISSUANCE_BUYBACK_CONVERSION_AND_SPLIT_ALIGNMENT_NOT_VERIFIED' in row['limitations']


def test_server_opt_in_is_required_and_paths_are_not_returned(tmp_path):
    assert LocalCapitalReference.from_environment({}) is None
    assert LocalCapitalReference.from_environment({'MIA_CAPITAL_REFERENCE_DIR': str(tmp_path)}) is None
    empty = LocalCapitalReference(tmp_path).read('../APP', NOW)
    assert str(tmp_path) not in json.dumps(empty)
    assert empty['status'] == 'UNAVAILABLE'


def test_actual_valuation_api_is_read_only_and_reference_never_enters_task_or_model(tmp_path):
    folder = tmp_path / 'private-captures'
    capture(folder)
    app = create_app(db_path=tmp_path/'isolated.db', market_fixture='evals/fixtures/market_eod.json',
        environment={'MIA_CAPITAL_REFERENCE_MODE': 'personal-display', 'MIA_CAPITAL_REFERENCE_DIR': str(folder)})
    t = task('SMR', loss=True)
    # Display a synthetic SMR response too; preserve unqualified computed inputs.
    capture(folder, 'SMR')
    sources = list(t.sources)
    price = sources[0].evidence
    data = {k: v for k, v in price.structured_data.items() if k != 'capital_basis_check'}
    sources[0] = sources[0].model_copy(update={'evidence': price.model_copy(update={
        'structured_data': data, 'content_hash': content_hash(data)})})
    t = t.model_copy(update={'sources': sources})
    store = app.state.research_service.store
    payload = t.model_dump(mode='json'); raw = json.dumps(payload, separators=(',', ':'))
    with store._connect() as db:
        db.execute('INSERT INTO research_tasks VALUES(?,?,?,?,?,?)',
            (t.task_id, 1, 'COMPLETE', NOW.isoformat(), content_hash(payload), raw))
        db.execute('INSERT INTO research_task_turns VALUES(?,?,?,?)',
            (t.task_id, 1, content_hash(payload), raw))
        before = db.execute('SELECT * FROM research_tasks').fetchall()
    with TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',50000)) as client:
        response = client.get(f'/api/research/tasks/{t.task_id}/valuation?revision=1')
        assert response.status_code == 200, response.text
        report = response.json()
        assert report['local_capital_reference']['value'] == '98765.432100'
        assert report['metrics']['market_cap_reference']['status'] == 'UNKNOWN'
        # EPS itself is blocked by capital-basis qualification, so no PE can be
        # inferred. Qualified negative-EPS NOT_MEANINGFUL has its own old oracle.
        assert report['metrics']['pe_ttm']['status'] == 'UNKNOWN'
        assert report['metrics']['price_sales_ttm']['status'] == 'NOT_MEANINGFUL'
        saved = client.get(f'/api/research/tasks/{t.task_id}').json()
        assert '98765.432100' not in json.dumps(saved)
        assert not any(s['evidence']['provider'] == 'massive' for s in saved['sources'])
        assert client.get(f'/api/research/tasks/{t.task_id}/valuation?revision=2').status_code == 404
        hidden=client.get(f'/api/research/tasks/{t.task_id}/valuation',headers={'X-Forwarded-For':'127.0.0.1'}).json()
        assert 'value' not in hidden['local_capital_reference']
    for base_url,host in [('http://127.0.0.1','192.0.2.1'),('http://public.example','127.0.0.1')]:
        with TestClient(app,base_url=base_url,client=(host,50000)) as client:
            hidden=client.get(f'/api/research/tasks/{t.task_id}/valuation').json()
            assert hidden['local_capital_reference']['reason']=='PERSONAL_DISPLAY_REQUIRES_DIRECT_LOOPBACK'
            assert 'value' not in hidden['local_capital_reference']
    with store._connect() as db:
        assert before == db.execute('SELECT * FROM research_tasks').fetchall()
        assert db.execute('SELECT COUNT(*) FROM query_runs').fetchone()[0] == 0
