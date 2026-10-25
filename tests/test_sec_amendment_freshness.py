"""F2: an amendment is never a first disclosure, even when its original is unseen.

`sec_filings` can only link an amendment to an original inside the lookback window.
Outside it the link is unknown, but the disclosure identity is still an amendment, so
freshness must not fall back to FRESH / FIRST_DISCLOSURE.

Real SEC data behind these cases:
  DOW 8-K/A 0001193125-26-296205 filed 2026-07-06 (item 5.02);
      original 8-K 0001193125-26-153769 filed 2026-04-14 (83 days earlier).
  KR  8-K/A 0001104659-25-118905 filed 2025-12-05 (item 2.06);
      original 8-K 0001104659-25-113746 filed 2025-11-18 (17 days earlier).
"""
from datetime import datetime, timezone

import pytest

from market_agent.sec_filings import CapturedFilingsSource, project


def _capture(rows, *, symbol="UNP", cik="0000100885", name="UNION PACIFIC CORP"):
    from sec_capture_fixtures import verified_test_row
    rows = [verified_test_row(row,cik) for row in rows]
    return {
        "schema_version": "ivs-sec-capture-v1",
        "captured_at": "2026-10-24T15:18:42.990912+00:00",
        "source": "SEC_EDGAR_SUBMISSIONS",
        "source_uri": f"https://data.sec.gov/submissions/CIK{cik}.json",
        "ticker_map_uri": "https://www.sec.gov/files/company_tickers.json",
        "issuer": {"symbol": symbol, "cik": cik, "name": name, "tickers": [symbol]},
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
        "filing_date": "2026-10-22",
        "primary_document": "unp-20261022.htm",
        "items": ["2.02"],
        "raw_metadata_hash": "sha256:x",
    }
    row.update(overrides)
    return row


def _project(capture, *, as_of, symbol="UNP", lookback_days=7):
    return project(CapturedFilingsSource(capture), as_of=datetime.fromisoformat(as_of),
                   scopes=[(symbol, "HOLDING")], lookback_days=lookback_days,
                   capture_identity="test")


def test_an_amendment_whose_original_is_outside_the_window_is_not_a_first_disclosure():
    amendment = _row(accession="0001193125-26-296205", form="8-K/A", base_form="8-K",
                     acceptance="2026-07-06T20:00:00.000Z", filing_date="2026-07-06",
                     items=["5.02"], primary_document="dow-20260706.htm")
    projection = _project(_capture([amendment], symbol="KR", cik="0000056873", name="KROGER CO"),
                          as_of="2026-07-06T23:59:59+00:00", symbol="KR")
    filing = projection.filings[0]
    assert filing.is_amendment is True
    assert filing.form == "8-K/A"
    assert filing.amends is None
    assert filing.ranking.fresh_disclosure is False
    assert "FIRST_DISCLOSURE" not in filing.ranking.basis
    assert "AMENDMENT_ORIGINAL_OUTSIDE_WINDOW" in filing.ranking.basis
    # Independent accession identity and the amendment count are both preserved.
    assert filing.evidence_id == "sec:0000056873:0001193125-26-296205"
    assert projection.counts.amendments == 1
    assert any("修订申报" in item for item in projection.limitations)


def test_a_linked_amendment_keeps_its_relation_and_is_not_fresh():
    rows = [_row(accession="0000100885-26-000410", form="8-K",
                 acceptance="2026-10-20T12:00:00.000Z", filing_date="2026-10-20", items=["2.02"]),
            _row(accession="0000100885-26-000411", form="8-K/A",
                 acceptance="2026-10-22T12:00:00.000Z", filing_date="2026-10-22", items=["2.02"])]
    projection = _project(_capture(rows), as_of="2026-10-24T00:00:00+00:00")
    amendment = next(item for item in projection.filings if item.form == "8-K/A")
    assert amendment.amends.accession == "0000100885-26-000410"
    assert amendment.ranking.fresh_disclosure is False
    assert "FIRST_DISCLOSURE" not in amendment.ranking.basis
    assert "AMENDMENT_ORIGINAL_OUTSIDE_WINDOW" not in amendment.ranking.basis


def test_a_plain_8k_is_still_a_first_disclosure():
    projection = _project(_capture([_row()]), as_of="2026-10-24T00:00:00+00:00")
    filing = projection.filings[0]
    assert filing.ranking.fresh_disclosure is True
    assert "FIRST_DISCLOSURE" in filing.ranking.basis


def test_a_follow_on_is_still_not_fresh():
    rows = [_row(accession="0000100885-26-000420", form="8-K",
                 acceptance="2026-10-18T12:00:00.000Z", filing_date="2026-10-18", items=["2.02"]),
            _row(accession="0000100885-26-000421", form="8-K",
                 acceptance="2026-10-22T12:00:00.000Z", filing_date="2026-10-22", items=["2.02"])]
    projection = _project(_capture(rows), as_of="2026-10-24T00:00:00+00:00")
    newer = next(item for item in projection.filings if item.accession == "0000100885-26-000421")
    assert newer.follows.accession == "0000100885-26-000420"
    assert newer.ranking.fresh_disclosure is False
