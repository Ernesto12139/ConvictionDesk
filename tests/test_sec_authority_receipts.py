"""Negative proofs for the actual public capture adapter and metadata verifier."""
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path
import urllib.error
import pytest
from market_agent.sec import _hash, filing_metadata_hash
from market_agent.sec_filings import CapturedFilingsSource, project

ROOT=Path(__file__).resolve().parents[1]
AS_OF=datetime.fromisoformat('2026-10-24T20:00:00+00:00')

def capture():
    data=json.loads((ROOT/'evals/fixtures/ivs_sec/UNP.json').read_text(encoding='utf-8'))
    data['rows']=[next(row for row in data['rows'] if row['accession']=='0000100885-26-000319')]
    return data

def report(data):
    return CapturedFilingsSource(data).recent_filings('UNP',as_of=AS_OF)

def test_rehashing_shifted_raw_and_normalized_times_cannot_self_verify():
    data=capture();row=data['rows'][0]
    for key in ('acceptance','raw_submission_acceptance','verified_edgar_accepted_at_utc'):
        row[key]=(datetime.fromisoformat(row[key].replace('Z','+00:00'))+timedelta(hours=4)).isoformat()
    row['raw_metadata_hash']=filing_metadata_hash(accession=row['accession'],form=row['form'],
        acceptance=row['raw_submission_acceptance'],filing_date=row['filing_date'],
        items=','.join(row['items']),primary_document=row['primary_document'])
    data['raw_metadata_hash']=_hash(data['rows'])
    assert report(data).rows == []
    assert report(data).excluded[0].reason == 'TIME_NOT_INDEPENDENTLY_VERIFIED'

@pytest.mark.parametrize('field,value', [('source_uri','https://example.org/index'),
    ('source_sha256','sha256:bad'),('observed_at','2026-10-25T12:00:00'),
    ('source_metadata_hash','sha256:'+'f'*64),('accepted_eastern','2026-10-22 11:46:52')])
def test_unbound_authority_receipts_are_refused(field,value):
    data=capture();data['rows'][0]['acceptance_authority'][field]=value
    assert report(data).rows == []

def test_corrected_pit_between_true_and_old_shifted_time_includes_filing():
    data=capture()
    result=project(CapturedFilingsSource(data),as_of=datetime.fromisoformat('2026-10-22T12:16:52+00:00'),scopes=[('UNP','HOLDING')])
    assert [f.accession for f in result.filings] == ['0000100885-26-000319']
    provenance=result.filings[0].provenance
    assert provenance.verification_status == 'VERIFIED_INDEX_AND_SUBMISSIONS_AGREE'
    assert provenance.raw_submission_acceptance == '2026-10-22T11:46:52.000Z'
    assert provenance.verified_edgar_accepted_at_utc == datetime.fromisoformat('2026-10-22T11:46:52+00:00')
    assert provenance.verification_source_hash.startswith('sha256:')
    assert provenance.verification_observed_at is not None

def verifier():
    spec=importlib.util.spec_from_file_location('capture_verifier_under_test',ROOT/'scripts/verify_sec_capture.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

def test_verifier_refusal_stops_session_without_retry(monkeypatch):
    module=verifier();calls=[]
    class Opener:
        def open(self,request,timeout):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url,429,'refused',{},None)
    monkeypatch.setattr(module.urllib.request,'build_opener',lambda *args:Opener())
    monkeypatch.setattr(module.time,'sleep',lambda seconds:None)
    with pytest.raises(SystemExit,match='SEC_REFUSED_SESSION_STOPPED'):
        module._get('https://www.sec.gov/files/company_tickers.json')
    assert len(calls)==1

def test_verifier_never_follows_redirects():
    assert verifier().NoRedirect().redirect_request(None,None,302,'',{},'https://example.org') is None

@pytest.mark.parametrize('wall', ['2026-03-08T02:30:00','2026-11-01T01:30:00'])
def test_ambiguous_or_nonexistent_dst_clock_is_not_an_instant(wall):
    with pytest.raises(ValueError,match='EDGAR_AMBIGUOUS_WALL_TIME'):
        verifier().to_utc(datetime.fromisoformat(wall))

def test_each_issuer_has_edgar_authority_anchors_in_est_and_edt():
    from market_agent.sec import SEC_TIMEZONE
    from market_agent.sec_filings import verified_capture_time
    offsets={symbol:set() for symbol in ('UNP','KR','DOW')}
    for symbol in offsets:
        data=json.loads((ROOT/f'evals/fixtures/ivs_sec/{symbol}.json').read_text(encoding='utf-8'))
        for row in data['rows']:
            instant,precision=verified_capture_time(row,data['issuer']['cik'])
            assert precision=='SECOND'
            offsets[symbol].add(instant.astimezone(SEC_TIMEZONE).utcoffset().total_seconds()/3600)
    extra=json.loads((ROOT/'evals/fixtures/sec_acceptance_dst_anchors.json').read_text(encoding='utf-8'))
    for anchor in extra['anchors']:
        instant,precision=verified_capture_time(anchor['row'],anchor['cik'])
        assert precision=='SECOND'
        offsets[anchor['symbol']].add(instant.astimezone(SEC_TIMEZONE).utcoffset().total_seconds()/3600)
    assert offsets=={'UNP':{-4,-5},'KR':{-4,-5},'DOW':{-4,-5}}
