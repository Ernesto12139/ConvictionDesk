"""Synthetic source receipts for unit tests, never real SEC verification evidence."""
from datetime import datetime
from market_agent.sec import SEC_TIMEZONE, filing_metadata_hash, normalize_acceptance

def verified_test_row(row, cik):
    instant, precision = normalize_acceptance(row.get('acceptance'))
    if precision != 'SECOND':
        return row
    accession=row['accession']
    source_hash=filing_metadata_hash(accession=accession,form=row['form'],acceptance=row['acceptance'],
        filing_date=row.get('filing_date'),items=','.join(row.get('items') or []),primary_document=row.get('primary_document'))
    row.update(raw_submission_acceptance=row['acceptance'],verified_edgar_accepted_at_utc=row['acceptance'],
        time_verification='VERIFIED_INDEX_AND_SUBMISSIONS_AGREE',raw_metadata_hash=source_hash,
        acceptance_authority={'source_uri':f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-','')}/{accession}-index.htm",
            'source_sha256':'sha256:'+'0'*64, 'accepted_eastern':instant.astimezone(SEC_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S'),
            'observed_at':'2027-01-01T00:00:00+00:00','source_metadata_hash':source_hash})
    return row
