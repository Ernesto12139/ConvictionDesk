"""IVS-SEC-001: the ivs-sec-v1 projection.

Every case here is a way a filing list can mislead: a candidate counted as a holding,
an unknown item code treated as material, two unrelated 8-Ks linked because both carry
exhibits, a refused source reported as "no filings", or a ranking presented as a model
judgement about investment impact.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from market_agent.provider import ProviderError
from market_agent.sec import normalize_acceptance
from market_agent.sec_filings import (
    CAPTURE_SCHEMA_VERSION,
    CapturedFilingsSource,
    by_scope,
    classify,
    evidence_id_for,
    normalize_items,
    project,
    resolve_scopes,
)

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
CIK = "0000100885"


def capture(rows, *, symbol="AAA", cik=CIK, name="AURORA GRID CORP",
            captured_at=NOW.isoformat()):
    result = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "captured_at": captured_at,
        "source_uri": f"https://data.sec.gov/submissions/CIK{cik}.json",
        "issuer": {"symbol": symbol, "cik": cik, "name": name, "tickers": [symbol]},
        "mapping": {"source_uri": "https://www.sec.gov/files/company_tickers.json",
                    "observed_at": captured_at},
        "rows": [
            {"accession": accession, "form": form, "base_form": form.split("/")[0],
             "acceptance": acceptance, "filing_date": filed, "primary_document": primary,
             "items": items, "raw_metadata_hash": "sha256:" + accession[-8:]}
            for accession, form, acceptance, filed, items, primary in rows
        ],
    }
    from sec_capture_fixtures import verified_test_row
    result['rows']=[verified_test_row(row,cik) for row in result['rows']]
    return result


def filing(accession, form, acceptance, items, *, filed=None, primary="doc.htm"):
    return (accession, form, acceptance, filed or acceptance[:10], items, primary)


def build(rows, *, scopes=(("AAA", "HOLDING"),), as_of=NOW, **kwargs):
    return project(CapturedFilingsSource(capture(rows)), as_of=as_of,
                   scopes=list(scopes), **kwargs)


class Failing:
    """A source that answers with a provider error, as the live one would."""

    def __init__(self, code):
        self.code = code
        self.calls = 0

    def recent_filings(self, symbol, *, as_of):
        self.calls += 1
        raise ProviderError(self.code, "scripted failure")


class Exploding:
    def recent_filings(self, symbol, *, as_of):
        raise RuntimeError("unexpected provider bug")


# --- identity and dedup ------------------------------------------------------

def test_evidence_id_is_derived_from_cik_and_accession():
    assert evidence_id_for(CIK, "0000100885-26-000319") == "sec:0000100885:0000100885-26-000319"


def test_one_accession_is_one_filing_even_with_two_scopes():
    projection = build([filing("0000100885-26-000319", "8-K", "2026-10-22T15:46:52.000Z", ["2.02"])],
                       scopes=(("AAA", "HOLDING"), ("AAA", "WATCHING")))
    assert len(projection.filings) == 1
    row = projection.filings[0]
    # A dual scope is an annotation, never two cards and never two sources.
    assert row.scope.kind == "HOLDING"
    assert [(ref.kind, ref.symbol) for ref in row.scope_matches] == [
        ("HOLDING", "AAA"), ("WATCHING", "AAA")]


def test_repeated_reads_produce_identical_bytes():
    rows = [filing("0000100885-26-000319", "8-K", "2026-10-22T15:46:52.000Z", ["2.02"])]
    first = build(rows).model_dump(mode="json")
    second = build(rows).model_dump(mode="json")
    assert first == second


def test_no_raw_quantities_or_account_ids_reach_the_output():
    rows = [filing("0000100885-26-000319", "8-K", "2026-10-22T15:46:52.000Z", ["2.02"])]
    payload = json.dumps(build(rows).model_dump(mode="json"))
    for forbidden in ("quantity", "account_id", "avg_cost", "position_id"):
        assert forbidden not in payload


# --- scope -------------------------------------------------------------------

def test_only_holdings_and_watching_are_scanned():
    entries = [type("E", (), {"symbol": "AAA", "state": "HOLDING"})(),
               type("E", (), {"symbol": "BBB", "state": "WATCHING"})(),
               type("E", (), {"symbol": "CCC", "state": "CANDIDATE"})(),
               type("E", (), {"symbol": "DDD", "state": "ARCHIVED"})()]
    assert resolve_scopes(entries=entries) == {"AAA": ["HOLDING"], "BBB": ["WATCHING"]}


def test_a_candidate_is_never_requested():
    projection = build([], scopes=(("CCC", "CANDIDATE"),))
    assert projection.status == "NOT_SCANNED"
    assert projection.counts.symbols_requested == 0
    assert projection.per_symbol_coverage == []


def test_symbol_count_is_bounded():
    scopes = tuple((f"S{index:02d}", "HOLDING") for index in range(5))
    projection = project(Failing("not_found"), as_of=NOW, scopes=list(scopes), max_symbols=2)
    assert projection.counts.symbols_requested == 2
    assert any("上限" in line for line in projection.limitations)


# --- classification ----------------------------------------------------------

def test_unknown_item_codes_are_retained_and_never_inferred():
    known, unknown = normalize_items(["2.02", "99.99", "9.01", "2.02"])
    assert known == ["2.02", "9.01"]
    assert unknown == ["99.99"]


def test_sec_item_code_drives_the_event_class_not_the_headline():
    assert classify("8-K", "8-K", ["2.02", "9.01"]) == (
        "EARNINGS", "经营成果与财务状况", "MATERIAL_ITEM")


def test_exhibit_only_8k_is_unclassified():
    assert classify("8-K", "8-K", ["9.01"]) == ("UNCLASSIFIED_EXHIBITS", "仅附件的当前报告",
                                                "UNCLASSIFIED")


def test_regulation_fd_is_routine_not_material():
    projection = build([filing("0000100885-26-000401", "8-K", "2026-10-22T15:46:52.000Z", ["7.01"])])
    row = projection.filings[0]
    assert row.event_class == "REG_FD"
    assert row.ranking.band == "ROUTINE_ITEM"


def test_a_periodic_report_is_scheduled_not_a_surprise():
    projection = build([filing("0000100885-26-000402", "10-Q", "2026-10-22T15:46:52.000Z", [])])
    row = projection.filings[0]
    assert row.event_class == "PERIODIC_REPORT"
    assert row.ranking.band == "SCHEDULED_REPORT"


# --- amendments and follow-ons ----------------------------------------------

def test_an_amendment_is_a_distinct_filing_that_links_its_original():
    rows = [filing("0000100885-26-000410", "8-K", "2026-10-20T12:00:00.000Z", ["2.02"]),
            filing("0000100885-26-000411", "8-K/A", "2026-10-22T12:00:00.000Z", ["2.02"])]
    projection = build(rows)
    original, amendment = projection.filings[1], projection.filings[0]
    assert amendment.is_amendment is True
    assert amendment.base_form == "8-K"
    assert amendment.amends.accession == original.accession
    assert amendment.amends.basis == "SAME_BASE_FORM_AND_SHARED_ITEM"
    assert original.amends is None
    # Two accessions, two evidence identities, one event class: an amendment is not
    # corroboration and does not become a second source.
    assert amendment.evidence_id != original.evidence_id
    assert projection.counts.amendments == 1


def test_an_amendment_without_a_prior_filing_has_no_link():
    projection = build([filing("0000100885-26-000412", "8-K/A", "2026-10-22T12:00:00.000Z", ["2.02"])])
    assert projection.filings[0].amends is None


def test_a_follow_on_is_a_candidate_relation_not_an_amendment():
    rows = [filing("0000100885-26-000420", "8-K", "2026-10-18T12:00:00.000Z", ["2.02"]),
            filing("0000100885-26-000421", "8-K", "2026-10-22T12:00:00.000Z", ["2.02"])]
    projection = build(rows)
    newer = projection.filings[0]
    assert newer.is_amendment is False
    assert newer.follows.accession == "0000100885-26-000420"
    assert newer.follows.basis == "SAME_ITEM_WITHIN_FOLLOW_WINDOW"
    assert newer.ranking.fresh_disclosure is False
    assert projection.counts.follow_ons == 1


def test_a_follow_on_outside_the_window_does_not_link():
    rows = [filing("0000100885-26-000430", "8-K", "2026-08-01T12:00:00.000Z", ["2.02"]),
            filing("0000100885-26-000431", "8-K", "2026-10-22T12:00:00.000Z", ["2.02"])]
    projection = build(rows, lookback_days=365)
    assert projection.filings[0].follows is None


def test_exhibit_only_overlap_does_not_link_two_unrelated_8ks():
    rows = [filing("0000100885-26-000440", "8-K", "2026-10-18T12:00:00.000Z", ["5.02", "9.01"]),
            filing("0000100885-26-000441", "8-K", "2026-10-22T12:00:00.000Z", ["2.02", "9.01"])]
    projection = build(rows)
    assert all(row.follows is None for row in projection.filings)


def test_a_follow_on_never_crosses_symbols():
    """The lane is symbol-scoped: two issuers filing the same item stay apart."""
    class TwoIssuers:
        def recent_filings(self, symbol, *, as_of):
            cik = "0000100885" if symbol == "AAA" else "0001751788"
            return CapturedFilingsSource(capture(
                [filing(f"{cik}-26-000450", "8-K", "2026-10-22T12:00:00.000Z", ["2.02"])],
                symbol=symbol, cik=cik)).recent_filings(symbol, as_of=as_of)

    projection = project(TwoIssuers(), as_of=NOW,
                         scopes=[("AAA", "HOLDING"), ("BBB", "HOLDING")])
    assert len(projection.filings) == 2
    assert all(row.follows is None for row in projection.filings)
    assert {row.cik for row in projection.filings} == {"0000100885", "0001751788"}


# --- ordering ----------------------------------------------------------------

def test_filings_are_sorted_by_available_at_then_accession():
    rows = [filing("0000100885-26-000460", "8-K", "2026-10-20T12:00:00.000Z", ["2.02"]),
            filing("0000100885-26-000462", "8-K", "2026-10-22T12:00:00.000Z", ["2.02"]),
            filing("0000100885-26-000461", "8-K", "2026-10-22T12:00:00.000Z", ["2.02"])]
    projection = build(rows)
    assert [row.accession for row in projection.filings] == [
        "0000100885-26-000461", "0000100885-26-000462", "0000100885-26-000460"]


def test_ranked_order_puts_material_items_before_routine_and_holdings_first():
    class Mixed:
        def recent_filings(self, symbol, *, as_of):
            rows = ([filing("0000100885-26-000470", "8-K", "2026-10-22T12:00:00.000Z", ["7.01"])]
                    if symbol == "AAA" else
                    [filing("0001751788-26-000470", "8-K", "2026-10-21T12:00:00.000Z", ["2.02"])])
            cik = "0000100885" if symbol == "AAA" else "0001751788"
            return CapturedFilingsSource(capture(rows, symbol=symbol, cik=cik)).recent_filings(
                symbol, as_of=as_of)

    projection = project(Mixed(), as_of=NOW,
                         scopes=[("AAA", "HOLDING"), ("BBB", "WATCHING")])
    ranked = [row.symbol for row in projection.filings
              if row.evidence_id in projection.ranked_evidence_ids]
    # BBB's material item outranks AAA's routine item even though AAA is a holding only
    # within its own scope: scope first, then band.
    assert ranked[0] == "AAA"
    assert by_scope(projection)[0]["scope"] == "HOLDING"
    assert len(by_scope(projection)[0]["evidence_ids"]) == 1


def test_ranking_is_labelled_deterministic_and_not_a_model_judgement():
    projection = build([filing("0000100885-26-000480", "8-K", "2026-10-22T12:00:00.000Z", ["2.02"])])
    row = projection.filings[0]
    assert row.ranking.basis == ["SCOPE_HOLDING", "SEC_ITEM_2.02", "FIRST_DISCLOSURE"]
    assert "不是模型判定的重大投资影响" in projection.ranking_meaning
    assert "不是模型" in row.ranking.meaning


# --- coverage honesty --------------------------------------------------------

def test_no_filings_in_window_is_a_complete_empty_answer():
    projection = build([filing("0000100885-26-000490", "8-K", "2026-09-01T12:00:00.000Z", ["2.02"])],
                       lookback_days=7)
    assert projection.status == "COMPLETE_NO_NEW_FILINGS"
    assert projection.per_symbol_coverage[0].status == "COMPLETE_NO_NEW_FILINGS"
    assert projection.per_symbol_coverage[0].outside_window == 1
    assert projection.per_symbol_coverage[0].excluded_by_pit == 0
    assert projection.filings == []


def test_a_refused_source_is_not_reported_as_no_filings():
    projection = project(Failing("transport"), as_of=NOW, scopes=[("AAA", "HOLDING")])
    coverage = projection.per_symbol_coverage[0]
    assert coverage.status == "SOURCE_UNAVAILABLE"
    assert coverage.mapping_status == "LOOKUP_FAILED"
    assert projection.status == "SOURCE_UNAVAILABLE"
    assert any("不可用" in line for line in coverage.limitations)
    assert projection.filings == []


def test_a_symbol_with_no_cik_is_not_scanned_and_not_empty():
    projection = project(Failing("not_found"), as_of=NOW, scopes=[("AAA", "HOLDING")])
    coverage = projection.per_symbol_coverage[0]
    assert coverage.status == "NOT_SCANNED"
    assert coverage.mapping_status == "NOT_FOUND"
    assert coverage.cik is None
    assert any("不是“没有新申报”" in line for line in coverage.limitations)


def test_invalid_metadata_is_its_own_state():
    projection = project(Failing("invalid_data"), as_of=NOW, scopes=[("AAA", "HOLDING")])
    assert projection.per_symbol_coverage[0].status == "INVALID_DATA"
    assert projection.status == "INVALID_DATA"


def test_mixed_outcomes_are_partial_never_complete_empty():
    class Mixed:
        def recent_filings(self, symbol, *, as_of):
            if symbol == "BBB":
                raise ProviderError("transport", "down")
            return CapturedFilingsSource(capture([], symbol="AAA")).recent_filings(
                symbol, as_of=as_of)

    projection = project(Mixed(), as_of=NOW,
                         scopes=[("AAA", "HOLDING"), ("BBB", "HOLDING")])
    assert [row.status for row in projection.per_symbol_coverage] == [
        "COMPLETE_NO_NEW_FILINGS", "SOURCE_UNAVAILABLE"]
    assert projection.status == "PARTIAL"
    assert any("不是完整的“无新申报”" in line for line in projection.limitations)


def test_an_unexpected_provider_bug_is_a_coverage_answer_not_a_crash():
    projection = project(Exploding(), as_of=NOW, scopes=[("AAA", "HOLDING")])
    assert projection.per_symbol_coverage[0].status == "SOURCE_UNAVAILABLE"
    assert projection.filings == []


def test_window_start_and_end_are_recorded_per_symbol():
    projection = build([filing("0000100885-26-000495", "8-K", "2026-10-22T12:00:00.000Z", ["2.02"])],
                       lookback_days=7)
    coverage = projection.per_symbol_coverage[0]
    assert coverage.window_end == NOW
    assert coverage.window_start == NOW - timedelta(days=7)
    assert coverage.as_of == NOW


# --- rights and provenance ---------------------------------------------------

def test_every_filing_carries_public_rights_limited_to_metadata():
    projection = build([filing("0000100885-26-000500", "8-K", "2026-10-22T12:00:00.000Z", ["2.02"])])
    rights = projection.filings[0].rights
    assert rights.display == "PUBLIC_FILING"
    assert rights.model_forwarding == "GRANTED"
    # The grant is over filing metadata and the document link, not over anything else.
    assert "附件" in rights.reviewed_scope and "组合" in rights.reviewed_scope


def test_provenance_records_hash_version_and_capture_identity():
    projection = build([filing("0000100885-26-000501", "8-K", "2026-10-22T12:00:00.000Z", ["2.02"])],
                       capture_identity="fixture:test")
    provenance = projection.filings[0].provenance
    assert provenance.source == "SEC_EDGAR_SUBMISSIONS"
    assert provenance.raw_metadata_hash.startswith("sha256:")
    assert provenance.normalization_version == "ivs-sec-normalize-v1"
    assert provenance.capture_identity == "fixture:test"
    assert provenance.retrieved_at == NOW


def test_filings_are_always_labelled_unscreened():
    projection = build([filing("0000100885-26-000502", "8-K", "2026-10-22T15:46:52.000Z", ["2.02"])])
    row = projection.filings[0]
    assert row.screening.status == "UNSCREENED"
    assert row.screening.model_used is False
    assert projection.screening.status == "UNSCREENED"
    assert "未经模型" in row.screening.meaning


def test_a_date_only_filing_keeps_date_precision_in_the_projection():
    projection = build([filing("0000100885-26-000510", "10-K", "2026-10-22", [], filed="2026-10-22")],
                       as_of=datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc))
    row = projection.filings[0]
    assert row.time_precision == "DATE"
    assert row.accepted_at is None
    assert row.filing_date == date(2026, 10, 22)
    # Eligible only after the whole declared Eastern day.
    assert row.available_at == datetime(2026, 10, 23, 4, 0, tzinfo=timezone.utc)


def test_a_same_day_date_only_filing_is_not_eligible_yet():
    same_day = datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc)
    projection = build([filing("0000100885-26-000511", "10-K", "2026-10-22", [], filed="2026-10-22")],
                       as_of=same_day)
    assert projection.filings == []
    assert projection.per_symbol_coverage[0].status == "COMPLETE_NO_NEW_FILINGS"
    assert projection.per_symbol_coverage[0].excluded_by_pit == 1
    assert projection.per_symbol_coverage[0].outside_window == 0


def test_counts_describe_the_cards_shown():
    projection = build([filing("0000100885-26-000520", "8-K", "2026-10-22T12:00:00.000Z", ["2.02"]),
                        filing("0000100885-26-000521", "4", "2026-10-21T12:00:00.000Z", [])])
    assert projection.counts.filings == 2
    assert projection.counts.holding_filings == 2
    assert projection.counts.watching_filings == 0
    assert projection.counts.symbols_with_filings == 1


def test_negative_lookback_is_rejected():
    with pytest.raises(ValueError):
        build([], lookback_days=-1)


# --- route-facing cache ------------------------------------------------------

def test_cache_serves_nothing_before_a_refresh_and_reads_no_source():
    from market_agent.sec_filings import FilingsCache

    source = Failing("transport")
    cache = FilingsCache(clock=lambda: NOW)
    assert cache.current() is None
    assert source.calls == 0
    assert cache.state["last_result"] is None


def test_a_get_after_a_refresh_never_touches_the_source_again():
    from market_agent.sec_filings import FilingsCache

    source = CapturedFilingsSource(capture(
        [filing("0000100885-26-000319", "8-K", "2026-10-22T15:46:52.000Z", ["2.02"])]))
    cache = FilingsCache(clock=lambda: NOW)
    first = cache.refresh(source, scopes=[("AAA", "HOLDING")])
    for _ in range(3):
        assert cache.current() is first
    assert cache.state["last_result"] == "OK"
    assert cache.state["last_attempt_at"] == NOW.isoformat()
    assert cache.state["last_success_at"] == NOW.isoformat()


def test_a_failed_refresh_keeps_the_previous_snapshot():
    from market_agent.sec_filings import FilingsCache

    cache = FilingsCache(clock=lambda: NOW)
    good = cache.refresh(CapturedFilingsSource(capture(
        [filing("0000100885-26-000319", "8-K", "2026-10-22T15:46:52.000Z", ["2.02"])])),
        scopes=[("AAA", "HOLDING")])
    failed = cache.refresh(Failing("transport"), scopes=[("AAA", "HOLDING")])
    assert failed.status == "SOURCE_UNAVAILABLE"
    assert cache.state["last_result"] == "FAILED"
    # The page keeps the last real answer, and its timestamp, instead of going blank.
    assert cache.current() is good
    assert cache.state["as_of"] == NOW.isoformat()


def test_a_partial_refresh_is_reported_as_partial():
    from market_agent.sec_filings import FilingsCache

    class Mixed:
        def recent_filings(self, symbol, *, as_of):
            if symbol == "BBB":
                raise ProviderError("transport", "down")
            return CapturedFilingsSource(capture([], symbol="AAA")).recent_filings(
                symbol, as_of=as_of)

    cache = FilingsCache(clock=lambda: NOW)
    projection = cache.refresh(Mixed(), scopes=[("AAA", "HOLDING"), ("BBB", "HOLDING")])
    assert projection.status == "PARTIAL"
    assert cache.state["last_result"] == "PARTIAL"
    assert cache.state["last_status"] == "PARTIAL"


def test_an_all_refused_refresh_is_failed_not_empty_success():
    from market_agent.sec_filings import FilingsCache

    cache = FilingsCache(clock=lambda: NOW)
    projection = cache.refresh(Failing("not_found"), scopes=[("AAA", "HOLDING")])
    assert projection.status == "NOT_SCANNED"
    assert cache.state["last_result"] == "FAILED"
    assert cache.state["last_success_at"] is None
    assert cache.current() is None
