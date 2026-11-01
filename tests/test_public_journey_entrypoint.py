"""The documented default server discovers the new journey without losing replay."""
from fastapi.testclient import TestClient
from market_agent import serve


def test_default_public_entrypoint_switches_isolated_journeys(tmp_path, monkeypatch):
    forbidden = tmp_path / 'owner-must-not-open.sqlite3'
    monkeypatch.setenv('MIA_DB_PATH', str(forbidden))
    monkeypatch.setenv('OPENAI_API_KEY', 'TEST_NOT_A_KEY')
    app = serve.build_public_workbench(tmp_path / 'public-demo')
    with TestClient(app, base_url='http://127.0.0.1:8804', client=('127.0.0.1', 1)) as client:
        from market_agent.unified_workbench import COOKIE
        client.cookies.set(COOKIE, 'OWNER_LOCAL')
        assert client.get('/api/public-demo').json()['access'] == 'SHARED_READ_ONLY'
        assert client.get('/api/today/pulse').json() == {
            'status': 'OFFLINE_REPLAY', 'scopes': {}, 'model_calls': 0}
        assert 'href="/portfolio-priority"' in client.get('/').text
        assert client.post('/api/research/tasks', json={}).status_code == 403
        selected = client.get('/portfolio-priority')
        assert selected.status_code == 200
        assert client.cookies.get(COOKIE) == 'OWNER_LOCAL'
        assert 'href="/public-replay"' in selected.text
        overview = client.get('/api/portfolio-priority').json()
        assert len(overview['accounts']) == 4
        assert client.post('/api/trade', json={}).status_code == 403
        assert client.get('/api/providers').status_code == 403
        assert client.post('/api/portfolio-priority/research', json={
            'symbol': 'APP', 'account_id': 'SYN-GROWTH', 'mode': 'FAST'},
            headers={'Origin': 'https://foreign.invalid'}).status_code == 403
        assert client.get('/public-replay').status_code == 200
        assert client.get('/api/public-demo').status_code == 200
        assert client.get('/api/portfolio-priority').status_code == 404
        client.cookies.delete(serve.PUBLIC_JOURNEY_COOKIE)
        client.cookies.set(serve.PUBLIC_JOURNEY_COOKIE, 'OWNER_LOCAL')
        assert client.get('/api/portfolio/current').status_code == 403
    assert not forbidden.exists()
    assert (tmp_path / 'public-demo/portfolio-priority').is_dir()
