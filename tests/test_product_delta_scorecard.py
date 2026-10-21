"""The read-only harness cannot be pointed at Owner8000 or external accounts."""
import pytest
import httpx

from evals.run_product_delta_scorecard import measure


@pytest.mark.parametrize('base',['http://127.0.0.1:8000','http://example.com:8031',
    'https://127.0.0.1:8031','http://127.0.0.1','http://localhost:8031'])
def test_rejected_targets_never_open_a_transport(monkeypatch,base):
    def forbidden(*args,**kwargs):raise AssertionError('Unqualified transport attempted')
    monkeypatch.setattr(httpx,'Client',forbidden)
    with pytest.raises(ValueError):measure(base)
