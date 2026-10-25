"""IVS-SEC-001: the public SEC filings interface, at its boundaries.

These tests exist because the interface is the contract. Every case here is a way a
filing reader can lie: a date treated as an instant, a naive timestamp assumed to be
UTC, a filing shown before it was knowable, a traversal in a document name, a
transport hiccup turned into "no filings".
"""

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from market_agent.persistence import Store
from market_agent.provider import ProviderError
from market_agent.sec import (
    SEC_TIMEZONE,
    SECEDGARProvider,
    eligibility_bound,
    filing_source_url,
    normalize_acceptance,
    parse_filing_date,
    start_of_disclosed_day,
)

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
CIK = "0000100885"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = f"https://data.sec.gov/submissions/CIK{CIK}.json"


def ticker_map(symbol="UNP", cik=100885, title="UNION PACIFIC CORP"):
    return {"fields": ["cik_str", "ticker", "title"],
            "0": {"cik_str": cik, "ticker": symbol, "title": title}}


def submissions(rows, *, name="UNION PACIFIC CORP"):
    """Build a SEC-shaped submissions payload from compact row tuples."""
    columns = {"accessionNumber": [], "form": [], "acceptanceDateTime": [],
               "primaryDocument": [], "filingDate": [], "items": []}
    for accession, form, accepted, primary, filed, items in rows:
        columns["accessionNumber"].append(accession)
        columns["form"].append(form)
        columns["acceptanceDateTime"].append(accepted)
        columns["primaryDocument"].append(primary)
        columns["filingDate"].append(filed)
        columns["items"].append(items)
    return {"cik": "100885", "name": name, "filings": {"recent": columns}}


def provider(tmp_path, *, payloads, calls=None, clock=lambda: NOW, **kwargs):
    """A provider wired to a scripted SEC, with throttling off for speed."""

    def handler(request):
        if calls is not None:
            calls.append(str(request.url))
        body = payloads.get(str(request.url))
        if body is None:
            return httpx.Response(404, text="not found")
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)

    return SECEDGARProvider(
        Store(tmp_path / "sec-provider.db"), "qa@example.org",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock, min_request_interval=0.0, retry_delay=0.0, **kwargs)


def standard(tmp_path, rows, *, calls=None, **kwargs):
    return provider(tmp_path, payloads={TICKERS_URL: ticker_map(), SUBMISSIONS_URL: submissions(rows)},
                    calls=calls, **kwargs)


# --- acceptance precision ----------------------------------------------------

def test_iso_acceptance_is_second_precision_utc():
    accepted, precision = normalize_acceptance("2026-10-22T15:46:52.000Z")
    assert precision == "SECOND"
    assert accepted == datetime(2026, 10, 22, 15, 46, 52, tzinfo=timezone.utc)


def test_provider_datetime_is_accepted_as_an_actual_instant():
    value = datetime(2026, 10, 22, 11, 46, 52, tzinfo=timezone(timedelta(hours=-4)))
    accepted, precision = normalize_acceptance(value)
    assert precision == "SECOND"
    assert accepted == datetime(2026, 10, 22, 15, 46, 52, tzinfo=timezone.utc)


def test_bare_date_is_date_precision_and_yields_no_instant():
    accepted, precision = normalize_acceptance("2026-10-22")
    assert (accepted, precision) == (None, "DATE")


def test_date_object_is_date_precision():
    assert normalize_acceptance(date(2026, 10, 22)) == (None, "DATE")


def test_naive_datetime_is_rejected_rather_than_assumed_utc():
    with pytest.raises(ValueError, match="timezone"):
        normalize_acceptance(datetime(2026, 10, 22, 15, 46, 52))


def test_naive_iso_string_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        normalize_acceptance("2026-10-22T15:46:52")


def test_empty_or_unparsable_acceptance_is_unknown_not_today():
    assert normalize_acceptance("") == (None, "UNKNOWN")
    assert normalize_acceptance(None) == (None, "UNKNOWN")
    assert normalize_acceptance("not-a-time") == (None, "UNKNOWN")


def test_date_only_row_is_eligible_only_after_the_whole_eastern_day():
    bound = eligibility_bound(None, "DATE", date(2026, 10, 22))
    # Midnight Eastern at the end of 2026-10-22 is 04:00Z on 2026-10-23.
    assert bound == datetime(2026, 10, 23, 4, 0, tzinfo=timezone.utc)
    assert bound > start_of_disclosed_day(date(2026, 10, 22))


def test_unknown_timezone_fails_closed():
    assert eligibility_bound(None, "DATE", date(2026, 10, 22), timezone=None) is None


def test_filing_date_is_parsed_separately_from_any_instant():
    assert parse_filing_date("2026-10-22") == date(2026, 10, 22)
    assert parse_filing_date(None) is None
    assert parse_filing_date("garbage") is None


# --- source URL safety ------------------------------------------------------

def test_source_url_is_bound_to_cik_and_accession():
    url = filing_source_url("https://www.sec.gov", CIK, "0000100885-26-000319", "unp-20261022.htm")
    assert url == ("https://www.sec.gov/Archives/edgar/data/100885/"
                   "000010088526000319/unp-20261022.htm")


def test_unsafe_primary_document_falls_back_to_the_bound_directory():
    for name in ("../../evil.htm", "https://evil.example/x.htm", "a b.htm", None):
        url = filing_source_url("https://www.sec.gov", CIK, "0000100885-26-000319", name)
        assert url == "https://www.sec.gov/Archives/edgar/data/100885/000010088526000319/"
        assert "evil" not in url


def test_source_url_refuses_a_bad_cik_or_accession():
    assert filing_source_url("https://www.sec.gov", "100885", "0000100885-26-000319", "a.htm") is None
    assert filing_source_url("https://www.sec.gov", CIK, "0000100885-000319", "a.htm") is None
    assert filing_source_url("", CIK, "0000100885-26-000319", "a.htm") is None


# --- public interface -------------------------------------------------------

def test_public_recent_filings_returns_typed_rows(tmp_path):
    rows = [("0000100885-26-000319", "8-K", "2026-10-22T15:46:52.000Z",
             "unp-20261022.htm", "2026-10-22", "2.02,9.01")]
    report = standard(tmp_path, rows).recent_filings("UNP", as_of=NOW)
    assert report.schema_version == "ivs-sec-provider-v1"
    assert report.issuer.cik == CIK and report.issuer.symbol == "UNP"
    assert report.issuer.name == "UNION PACIFIC CORP"
    assert report.source == "SEC_EDGAR_SUBMISSIONS"
    assert report.source_uri == SUBMISSIONS_URL
    row = report.rows[0]
    assert row.accession == "0000100885-26-000319"
    assert row.form == "8-K" and row.base_form == "8-K" and row.is_amendment is False
    assert row.items == ["2.02", "9.01"]
    assert row.time_precision == "SECOND"
    assert row.accepted_at == datetime(2026, 10, 22, 15, 46, 52, tzinfo=timezone.utc)
    assert row.filing_date == date(2026, 10, 22)
    assert row.available_at == row.accepted_at
    assert row.source_url.endswith("/unp-20261022.htm")
    assert row.raw_metadata_hash.startswith("sha256:")


def test_filing_dates_are_not_sliced_from_the_acceptance_instant(tmp_path):
    # Acceptance is after midnight UTC on the 23rd; the filing date is still the 22nd.
    rows = [("0000100885-26-000320", "8-K", "2026-10-23T02:10:00.000Z",
             "unp-20261023.htm", "2026-10-22", "2.02")]
    row = standard(tmp_path, rows).recent_filings("UNP", as_of=NOW).rows[0]
    assert row.accepted_at.date() == date(2026, 10, 23)
    assert row.filing_date == date(2026, 10, 22)


def test_a_date_only_row_is_excluded_on_its_own_day(tmp_path):
    rows = [("0000100885-26-000321", "10-K", "2026-10-22", "unp-20261022.htm", "2026-10-22", "")]
    same_day = datetime(2026, 10, 22, 20, 0, tzinfo=timezone.utc)
    report = standard(tmp_path, rows).recent_filings("UNP", as_of=same_day)
    assert report.rows == []
    assert [(item.accession, item.reason) for item in report.excluded] == [
        ("0000100885-26-000321", "AFTER_AS_OF")]
    later = datetime(2026, 10, 23, 6, 0, tzinfo=timezone.utc)
    report = standard(tmp_path, rows).recent_filings("UNP", as_of=later)
    assert report.rows[0].time_precision == "DATE"
    assert report.rows[0].accepted_at is None
    assert report.rows[0].filing_date == date(2026, 10, 22)


def test_a_filing_accepted_after_as_of_is_excluded_and_reported(tmp_path):
    rows = [("0000100885-26-000322", "8-K", "2026-10-24T18:00:00.000Z", "a.htm", "2026-10-24", "2.02")]
    report = standard(tmp_path, rows).recent_filings("UNP", as_of=NOW)
    assert report.rows == []
    assert report.excluded[0].reason == "AFTER_AS_OF"
    assert any("excluded" in line for line in report.limitations)


def test_unknown_precision_rows_are_excluded_not_guessed(tmp_path):
    rows = [("0000100885-26-000323", "8-K", "", "a.htm", None, "2.02")]
    report = standard(tmp_path, rows).recent_filings("UNP", as_of=NOW)
    assert report.rows == []
    assert report.excluded[0].reason == "TIME_PRECISION_UNKNOWN"


def test_a_repeated_accession_is_one_row(tmp_path):
    row = ("0000100885-26-000324", "8-K", "2026-10-22T15:46:52.000Z", "a.htm", "2026-10-22", "2.02")
    report = standard(tmp_path, [row, row]).recent_filings("UNP", as_of=NOW)
    assert len(report.rows) == 1


def test_rows_are_sorted_by_available_at_then_accession(tmp_path):
    rows = [
        ("0000100885-26-000330", "8-K", "2026-10-20T12:00:00.000Z", "c.htm", "2026-10-20", "2.02"),
        ("0000100885-26-000332", "8-K", "2026-10-22T12:00:00.000Z", "a.htm", "2026-10-22", "2.02"),
        ("0000100885-26-000331", "8-K", "2026-10-22T12:00:00.000Z", "b.htm", "2026-10-22", "2.02"),
    ]
    report = standard(tmp_path, rows).recent_filings("UNP", as_of=NOW)
    assert [row.accession for row in report.rows] == [
        "0000100885-26-000331", "0000100885-26-000332", "0000100885-26-000330"]


def test_amendment_keeps_its_base_form_and_flag(tmp_path):
    rows = [("0000100885-26-000340", "8-K/A", "2026-10-22T15:46:52.000Z", "a.htm", "2026-10-22", "2.02")]
    row = standard(tmp_path, rows).recent_filings("UNP", as_of=NOW).rows[0]
    assert row.form == "8-K/A"
    assert row.base_form == "8-K"
    assert row.is_amendment is True


def test_cik_is_ten_digits_and_mapping_identity_is_retained(tmp_path):
    report = standard(tmp_path, []).recent_filings("UNP", as_of=NOW)
    assert report.issuer.cik == "0000100885"
    assert report.issuer.tickers == ["UNP"]
    assert report.issuer.mapping_source_uri.endswith("/files/company_tickers.json")
    assert report.issuer.mapping_observed_at == NOW


def test_second_read_is_served_from_cache_without_a_second_request(tmp_path):
    calls = []
    sec = standard(tmp_path, [], calls=calls)
    sec.recent_filings("UNP", as_of=NOW)
    first = list(calls)
    report = sec.recent_filings("UNP", as_of=NOW)
    assert calls == first
    assert report.from_cache is True


def test_cache_expires_and_refetches(tmp_path):
    calls = []
    clock = {"now": NOW}
    sec = standard(tmp_path, [], calls=calls, clock=lambda: clock["now"])
    sec.recent_filings("UNP", as_of=NOW)
    clock["now"] = NOW + timedelta(seconds=301)
    sec.recent_filings("UNP", as_of=clock["now"])
    assert calls.count(SUBMISSIONS_URL) == 2


def test_invalid_symbol_is_rejected_before_any_request(tmp_path):
    calls = []
    sec = standard(tmp_path, [], calls=calls)
    with pytest.raises(ProviderError) as caught:
        sec.recent_filings("not a ticker", as_of=NOW)
    assert caught.value.code == "invalid_input"
    assert calls == []


def test_naive_as_of_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="timezone"):
        standard(tmp_path, []).recent_filings("UNP", as_of=datetime(2026, 10, 24, 12, 0))


def test_unknown_symbol_is_not_found_not_empty(tmp_path):
    sec = provider(tmp_path, payloads={TICKERS_URL: ticker_map(symbol="OTHER")})
    with pytest.raises(ProviderError) as caught:
        sec.recent_filings("UNP", as_of=NOW)
    assert caught.value.code == "not_found"


# --- failure handling -------------------------------------------------------

def test_a_retryable_failure_is_retried_then_succeeds(tmp_path):
    calls = []
    state = {"attempts": 0}

    def handler(request):
        calls.append(str(request.url))
        if str(request.url) == TICKERS_URL:
            return httpx.Response(200, json=ticker_map())
        state["attempts"] += 1
        if state["attempts"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json=submissions([
            ("0000100885-26-000350", "8-K", "2026-10-22T15:46:52.000Z", "a.htm", "2026-10-22", "2.02")]))

    sec = SECEDGARProvider(Store(tmp_path / "retry.db"), "qa@example.org",
                           client=httpx.Client(transport=httpx.MockTransport(handler)),
                           clock=lambda: NOW, min_request_interval=0.0, retry_delay=0.0)
    report = sec.recent_filings("UNP", as_of=NOW)
    assert len(report.rows) == 1
    assert state["attempts"] == 2


def test_a_persistent_retryable_failure_surfaces_as_retryable(tmp_path):
    attempts = {"n": 0}

    def handler(request):
        if str(request.url) == TICKERS_URL:
            return httpx.Response(200, json=ticker_map())
        attempts["n"] += 1
        return httpx.Response(503, text="busy")

    sec = SECEDGARProvider(Store(tmp_path / "retry2.db"), "qa@example.org",
                           client=httpx.Client(transport=httpx.MockTransport(handler)),
                           clock=lambda: NOW, min_request_interval=0.0, retry_delay=0.0)
    with pytest.raises(ProviderError) as caught:
        sec.recent_filings("UNP", as_of=NOW)
    assert caught.value.code == "retryable"
    assert attempts["n"] == 2  # bounded, not an unbounded retry loop


def test_malformed_submissions_is_invalid_data(tmp_path):
    sec = provider(tmp_path, payloads={TICKERS_URL: ticker_map(),
                                       SUBMISSIONS_URL: {"filings": {"recent": {}}}})
    with pytest.raises(ProviderError) as caught:
        sec.recent_filings("UNP", as_of=NOW)
    assert caught.value.code == "invalid_data"


def test_misaligned_columns_are_invalid_data(tmp_path):
    payload = submissions([("0000100885-26-000360", "8-K", "2026-10-22T15:46:52.000Z",
                            "a.htm", "2026-10-22", "2.02")])
    payload["filings"]["recent"]["form"].append("10-Q")
    sec = provider(tmp_path, payloads={TICKERS_URL: ticker_map(), SUBMISSIONS_URL: payload})
    with pytest.raises(ProviderError) as caught:
        sec.recent_filings("UNP", as_of=NOW)
    assert caught.value.code == "invalid_data"


def test_naive_acceptance_from_the_provider_is_invalid_data_not_utc(tmp_path):
    payload = submissions([("0000100885-26-000361", "8-K", "2026-10-22T15:46:52",
                            "a.htm", "2026-10-22", "2.02")])
    sec = provider(tmp_path, payloads={TICKERS_URL: ticker_map(), SUBMISSIONS_URL: payload})
    with pytest.raises(ProviderError) as caught:
        sec.recent_filings("UNP", as_of=NOW)
    assert caught.value.code == "invalid_data"


def test_a_bad_accession_is_invalid_data(tmp_path):
    payload = submissions([("0000100885-000361", "8-K", "2026-10-22T15:46:52.000Z",
                            "a.htm", "2026-10-22", "2.02")])
    sec = provider(tmp_path, payloads={TICKERS_URL: ticker_map(), SUBMISSIONS_URL: payload})
    with pytest.raises(ProviderError) as caught:
        sec.recent_filings("UNP", as_of=NOW)
    assert caught.value.code == "invalid_data"


# --- compatibility with the retained callers --------------------------------

def test_legacy_recent_filings_shape_is_unchanged(tmp_path):
    rows = [("0000100885-26-000370", "10-K", "2026-10-22T15:46:52.000Z", "k.htm", "2026-10-22", "")]
    legacy = standard(tmp_path, rows)._recent_filings(CIK)
    assert set(legacy[0]) == {"accession_number", "form", "filed_at", "primary_document", "items"}
    assert legacy[0]["filed_at"] == datetime(2026, 10, 22, 15, 46, 52, tzinfo=timezone.utc)
    assert legacy[0]["items"] == ""


def test_legacy_view_uses_the_conservative_bound_for_date_only_rows(tmp_path):
    rows = [("0000100885-26-000371", "10-K", "2026-10-22", "k.htm", "2026-10-22", "")]
    legacy = standard(tmp_path, rows)._recent_filings(CIK)
    assert legacy[0]["filed_at"] == datetime(2026, 10, 23, 4, 0, tzinfo=timezone.utc)


def test_legacy_view_omits_a_row_with_no_usable_instant(tmp_path):
    rows = [("0000100885-26-000372", "8-K", "", "a.htm", None, "2.02")]
    assert standard(tmp_path, rows)._recent_filings(CIK) == []


def test_fundamentals_still_reads_the_legacy_shape(tmp_path):
    """The retained XBRL path must not change shape because of this interface."""
    payload = submissions([("0000100885-26-000380", "10-Q", "2026-10-10T12:00:00.000Z",
                            "q.htm", "2026-10-10", "")])
    facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        {"accn": "0000100885-26-000380", "form": "10-Q", "start": "2026-07-01",
         "end": "2026-09-30", "val": 100, "fy": 2026, "fp": "Q3"}]}}}}}
    sec = provider(tmp_path, payloads={
        TICKERS_URL: ticker_map(), SUBMISSIONS_URL: payload,
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json": facts})
    rows = sec.fundamentals("UNP", NOW)
    assert rows
