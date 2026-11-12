"""Public D104 shell is offline and cannot activate private runtime authority."""
import re
import socket

from fastapi.testclient import TestClient

from market_agent.serve import build_public_workbench


def test_five_destinations_and_truthful_market_without_credentials(tmp_path, monkeypatch):
    attempted = []

    def forbidden(address, *args, **kwargs):
        attempted.append(address)
        raise AssertionError('Public request attempted outbound transport')

    monkeypatch.setattr(socket, 'create_connection', forbidden)
    owner = tmp_path / 'owner-never-open.sqlite3'
    monkeypatch.setenv('MIA_DB_PATH', str(owner))
    monkeypatch.setenv('OPENAI_API_KEY', 'SYNTHETIC_NOT_A_CREDENTIAL')
    with TestClient(build_public_workbench(tmp_path / 'demo')) as client:
        html = client.get('/').text
        assert 'data-runtime-mode="PUBLIC_DEMO"' in html
        assert set(re.findall(r'data-nav="([^"]+)"', html)) == {
            'today', 'market', 'research', 'portfolio', 'settings'}
        assert 'href="/portfolio-priority"' in html
        nav = re.search(r'<nav aria-label="主要页面">(.*?)</nav>', html, re.S).group(1)
        assert '/portfolio-priority' not in nav
        for asset in re.findall(r'(?:src|href)="(/static/[^"?]+)', html):
            assert client.get(asset).status_code == 200, asset
        market = client.get('/api/market/eod?symbols=APP,BB&sessions=20').json()
        assert market['last_completed_session'] is None
        assert market['provider'] is None
        assert market['retrieved_at'] is None
        assert market['rights'] == 'PUBLIC_DEMO_UNAVAILABLE'
        assert all(row['status'] == 'UNAVAILABLE' and row['bars'] == []
                   for row in market['series'])
        for endpoint in ('/api/research/tasks', '/api/providers/verify',
                         '/api/local-observations/refresh', '/api/attention/scan'):
            assert client.post(endpoint, json={}).status_code == 403
        assert client.get('/api/research/admission').status_code == 404
        assert client.get('/api/providers').status_code == 404
        priority = client.get('/portfolio-priority').text
        assert 'href="/public-replay"' in priority
        assert 'portfolio-priority-reader.js' in priority
        assert client.get('/static/portfolio-priority-reader.js').status_code == 200
        assert client.get('/public-replay').status_code == 200
    assert attempted == []
    assert not owner.exists()
