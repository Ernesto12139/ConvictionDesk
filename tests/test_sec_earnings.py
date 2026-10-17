from datetime import datetime, timezone

import httpx

from market_agent.persistence import Store
from market_agent.sec import SECEDGARProvider


def test_earnings_release_survives_newer_events_and_fast_fetch_is_bounded(tmp_path):
    requests = []
    recent = {'accessionNumber':[f'0000000001-26-{n:06}' for n in range(7,0,-1)],
        'form':['8-K']*6+['10-Q'], 'acceptanceDateTime':[f'2026-08-{n:02}T12:00:00Z' for n in range(7,0,-1)],
        'primaryDocument':[f'filing{n}.htm' for n in range(7,0,-1)],'items':['5.02']*5+['2.02','']}
    def handler(request):
        requests.append(str(request.url))
        if request.url.path.endswith('company_tickers.json'):
            return httpx.Response(200,json={'0':{'ticker':'APP','cik_str':1}})
        if request.url.host=='data.sec.gov':
            return httpx.Response(200,json={'filings':{'recent':recent}})
        if request.url.path.endswith('filing2.htm'):
            return httpx.Response(200,text='<p>Results operations.</p><a href="app-ex991.htm">99.1</a>')
        if request.url.path.endswith('app-ex991.htm'):
            return httpx.Response(200,text='<h1>Quarterly outlook</h1><p>Guidance reflects advertiser demand and cash generation.</p>')
        raise AssertionError('Unexpected download '+str(request.url))
    cutoff = datetime(2026,8,8,tzinfo=timezone.utc)
    provider = SECEDGARProvider(Store(tmp_path/'sec.sqlite3'),'Synthetic qa@example.org',
        client=httpx.Client(transport=httpx.MockTransport(handler)),clock=lambda:cutoff)
    result = provider.fast_search('APP','guidance advertiser demand',['8-K','10-Q'],cutoff)
    assert len(requests)==4  # ticker + submissions + one primary + one release
    assert result.indexed_documents==2
    assert result.evidence and 'Guidance reflects advertiser demand' in result.evidence[0].excerpt
    assert result.evidence[0].source_uri.endswith('/000000000126000002/app-ex991.htm')
    assert result.evidence[0].published_at==datetime(2026,8,2,12,tzinfo=timezone.utc)


def test_exhibit_cannot_escape_accession_or_official_host():
    base = 'https://www.sec.gov/Archives/edgar/data/1/123/primary.htm'
    for href in ('https://evil.test/ex991.htm','../ex991.htm','/ex991.htm','ex991.htm?token=secret',
                 'https://www.sec.gov/Archives/edgar/data/1/456/ex991.htm'):
        assert SECEDGARProvider._earnings_exhibit(base,f'<a href="{href}">99.1</a>') is None
    assert SECEDGARProvider._earnings_exhibit(base,'<a href="ex99.1.htm">99.1</a>')=='ex99.1.htm'
