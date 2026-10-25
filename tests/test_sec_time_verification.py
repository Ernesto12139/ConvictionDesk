"""Acceptance-time authenticity and amendment freshness.

Anchors are SEC's own Eastern acceptance clock from the EDGAR filing index, which is
generated from the SGML <ACCEPTANCE-DATETIME> header. Each expected UTC instant below
was read from that header and from the filing index independently; both agreed.
Coverage: EDT and EST, 8-K, 8-K/A, 10-Q, Form 3 and Form 4.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from market_agent.sec import eligibility_bound, normalize_acceptance
from market_agent.sec_filings import (
    CapturedFilingsSource,
    project,
)

FIXTURES = Path(__file__).resolve().parents[1] / "evals" / "fixtures" / "ivs_sec"
VERIFIED = "VERIFIED_INDEX_AND_SUBMISSIONS_AGREE"

# (fixture file, accession, form, season, SGML Eastern clock, verified UTC)
ANCHORS = [
    ("UNP.json", "0000100885-26-000319", "8-K", "EDT", "2026-10-22 07:46:52", "2026-10-22T11:46:52Z"),
    ("UNP.json", "0000100885-26-000321", "10-Q", "EDT", "2026-10-22 15:45:41", "2026-10-22T19:45:41Z"),
    ("UNP.json", "0000100885-26-000314", "4", "EDT", "2026-10-20 11:14:34", "2026-10-20T15:14:34Z"),
    ("KR.json", "0001104659-26-106941", "8-K/A", "EDT", "2026-09-11 10:05:48", "2026-09-11T14:05:48Z"),
    ("DOW.json", "0001751788-26-000162", "3", "EDT", "2026-10-23 16:30:36", "2026-10-23T20:30:36Z"),
    ("DOW.json", "0001751788-26-000095", "4", "EST", "2026-03-02 17:17:03", "2026-03-02T22:17:03Z"),
    ("DOW.json", "0001751788-26-000090", "4", "EST", "2026-03-02 17:16:10", "2026-03-02T22:16:10Z"),
]


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def rows_by_accession(name):
    return {row["accession"]: row for row in load(name)["rows"]}


# --- the captured instant must be SEC's instant -----------------------------

@pytest.mark.parametrize("name,accession,form,season,et_clock,expected", ANCHORS)
def test_captured_acceptance_equals_edgar_verified_utc(name, accession, form, season, et_clock, expected):
    row = rows_by_accession(name)[accession]
    assert row["form"] == form
    assert row["time_verification"] == VERIFIED
    instant, precision = normalize_acceptance(row["acceptance"])
    assert precision == "SECOND"
    assert instant == datetime.fromisoformat(expected.replace("Z", "+00:00"))
    # The raw SEC string is preserved beside the verified instant, never overwritten.
    assert row["raw_submission_acceptance"].endswith("Z")
    assert datetime.fromisoformat(row["raw_submission_acceptance"].replace("Z", "+00:00")) == instant


@pytest.mark.parametrize("name,accession,form,season,et_clock,expected", ANCHORS)
def test_eastern_clock_converts_with_the_calendar_not_a_constant(name, accession, form, season, et_clock, expected):
    """EDT rows are UTC-4, EST rows UTC-5. A single constant cannot serve both."""
    from zoneinfo import ZoneInfo

    eastern = datetime.fromisoformat(et_clock).replace(tzinfo=ZoneInfo("America/New_York"))
    expected_utc = datetime.fromisoformat(expected.replace("Z", "+00:00"))
    assert eastern.astimezone(timezone.utc) == expected_utc
    assert eastern.utcoffset().total_seconds() / 3600 == (-4.0 if season == "EDT" else -5.0)


def test_no_captured_row_claims_second_precision_without_verification():
    for name in ("UNP.json", "KR.json", "DOW.json"):
        for row in load(name)["rows"]:
            if row["acceptance"] is not None:
                assert row["time_verification"] == VERIFIED, (name, row["accession"])


def test_every_captured_row_is_independently_verified():
    for name in ("UNP.json", "KR.json", "DOW.json"):
        capture = load(name)
        summary = capture["time_verification_summary"]
        assert summary[VERIFIED] == len(capture["rows"])
        assert summary["UNRESOLVED_SUBMISSIONS_DISAGREE"] == 0
        assert summary["UNVERIFIED_INDEX_UNAVAILABLE"] == 0


# --- the hash must bind what SEC actually returned --------------------------

def test_row_hash_reproduces_from_the_raw_submission_metadata():
    """The regression that F1 broke: capture and live read must agree by hash."""
    from market_agent.sec import filing_metadata_hash

    checked = 0
    for name in ("UNP.json", "KR.json", "DOW.json"):
        for row in load(name)["rows"]:
            expected = filing_metadata_hash(
                accession=row["accession"], form=row["form"],
                acceptance=row["raw_submission_acceptance"],
                filing_date=row["filing_date"], items=",".join(row["items"]),
                primary_document=row["primary_document"])
            assert row["raw_metadata_hash"] == expected, (name, row["accession"])
            checked += 1
    assert checked == 120


def test_capture_hash_is_the_hash_of_its_rows():
    from market_agent.sec import _hash

    for name in ("UNP.json", "KR.json", "DOW.json"):
        capture = load(name)
        assert capture["raw_metadata_hash"] == _hash(capture["rows"])


# --- an unverified row must not become a second-precision fact --------------

def _capture(rows, symbol="UNP", cik="0000100885"):
    return {
        "schema_version": "ivs-sec-capture-v1",
        "captured_at": "2026-10-24T15:18:42.990912+00:00",
        "source": "SEC_EDGAR_SUBMISSIONS",
        "source_uri": f"https://data.sec.gov/submissions/CIK{cik}.json",
        "ticker_map_uri": "https://www.sec.gov/files/company_tickers.json",
        "issuer": {"symbol": symbol, "cik": cik, "name": "UNION PACIFIC CORP", "tickers": [symbol]},
        "mapping": {"source_uri": "https://www.sec.gov/files/company_tickers.json",
                    "observed_at": "2026-10-24T15:18:42.990925+00:00"},
        "row_count_total": len(rows),
        "rows": rows,
        "rights": "SEC public filing metadata; no document body captured.",
        "raw_metadata_hash": "sha256:test",
    }


def _row(**overrides):
    row = {
        "accession": "0000100885-26-000900",
        "form": "8-K",
        "base_form": "8-K",
        "acceptance": "2026-10-22T11:46:52.000Z",
        "raw_submission_acceptance": "2026-10-22T11:46:52.000Z",
        "acceptance_verified_from": "EDGAR_INDEX_ACCEPTED_ET",
        "time_verification": VERIFIED,
        "filing_date": "2026-10-22",
        "primary_document": "unp-20261022.htm",
        "items": ["2.02"],
        "raw_metadata_hash": "sha256:x",
    }
    from sec_capture_fixtures import verified_test_row
    verified_test_row(row, '0000100885')
    row.update(overrides)
    return row


def _project(capture, as_of="2026-10-24T00:00:00+00:00"):
    source = CapturedFilingsSource(capture)
    return project(source, as_of=datetime.fromisoformat(as_of), scopes=[("UNP", "HOLDING")],
                   lookback_days=7, capture_identity="test")


def test_an_unresolved_row_is_excluded_with_its_own_reason():
    projection = _project(_capture([_row(time_verification="UNRESOLVED_SUBMISSIONS_DISAGREE",
                                          acceptance=None)]))
    coverage = projection.per_symbol_coverage[0]
    assert coverage.status == "COMPLETE_NO_NEW_FILINGS"
    assert coverage.filing_count == 0
    assert coverage.excluded_by_pit == 1
    assert projection.filings == []


def test_an_unverified_row_cannot_pose_as_second_precision_even_with_an_instant():
    """A capture that still carries a string but admits it is unverified must not win."""
    projection = _project(_capture([_row(
        time_verification="UNRESOLVED_SUBMISSIONS_DISAGREE",
        acceptance="2026-10-22T11:46:52.000Z")]))
    assert projection.filings == []
    assert projection.per_symbol_coverage[0].filing_count == 0


def test_a_verified_row_is_still_eligible():
    projection = _project(_capture([_row()]))
    assert projection.per_symbol_coverage[0].status == "COMPLETE_WITH_FILINGS"
    filing = projection.filings[0]
    assert filing.time_precision == "SECOND"
    assert filing.available_at == datetime(2026, 10, 22, 11, 46, 52, tzinfo=timezone.utc)


def test_a_legacy_capture_without_verification_is_not_a_second_precision_fact():
    legacy = _row()
    for key in ("raw_submission_acceptance", "acceptance_verified_from", "time_verification"):
        legacy.pop(key)
    projection = _project(_capture([legacy]))
    assert projection.filings == []
    assert projection.per_symbol_coverage[0].excluded_by_pit == 1
