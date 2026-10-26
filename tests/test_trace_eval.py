"""The eval harness must measure the product, not itself."""
from __future__ import annotations

from datetime import datetime, timezone

from evals.trace_eval import (
    GROUNDING,
    RELIABILITY,
    corpus,
    corpus_digest,
    evaluate,
    render_markdown,
    run_case,
)

AS_OF = datetime(2026, 10, 26, tzinfo=timezone.utc)


def test_corpus_is_frozen_sized_and_unique():
    cases = corpus()
    assert 20 <= len(cases) <= 40
    ids = [case.case_id for case in cases]
    assert len(ids) == len(set(ids))
    assert {case.family for case in cases} == {GROUNDING, RELIABILITY}
    assert corpus_digest() == corpus_digest()


def test_every_required_risk_category_is_represented():
    categories = {case.category for case in corpus()}
    required = {
        "wrong_ticker", "pit_breach", "fake_citation", "missing_evidence",
        "counterevidence_omission", "stale_price", "unknown_as_hold",
        "decimal_float", "provider_timeout", "rate_limit", "rights_consent",
        "budget_denial", "revision_overwrite", "duplicate_event", "invalid_data",
    }
    assert required <= categories


def test_evaluation_passes_and_reports_real_denominators():
    report = evaluate(as_of=AS_OF)
    assert report["totals"]["failed"] == 0, report["named_failures"]
    assert report["totals"]["pass_rate"] == f"{report['totals']['cases']}/{report['totals']['cases']}"
    for family in (GROUNDING, RELIABILITY):
        row = report["families"][family]
        assert row["cases"] > 0
        assert row["pass_rate"] == f"{row['passed']}/{row['cases']}"
        assert row["failed_case_ids"] == []
        assert row["latency_p50_ms"] is not None
    assert report["dataset_digest"] == corpus_digest()
    assert report["code"]["commit"]
    assert report["command"]


def test_report_refuses_to_claim_investment_accuracy():
    report = evaluate(as_of=AS_OF)
    claims = report["claims"]
    assert claims["investment_accuracy_measured"] is False
    assert claims["alpha_measured"] is False
    assert claims["model_calls"] == 0
    assert claims["network_reads"] == 0
    assert claims["paid_dispatch_usd"] == "0.00"


def test_known_issue_is_surfaced_not_hidden():
    report = evaluate(as_of=AS_OF)
    ids = [issue["case_id"] for issue in report["known_issues"]]
    assert "G12-item-code-as-number" in ids
    note = next(issue["note"] for issue in report["known_issues"]
                if issue["case_id"] == "G12-item-code-as-number")
    assert "5.02" in note


def test_a_wrong_expectation_is_detected():
    from evals.trace_eval import Case, load_capture

    capture = load_capture()
    wrong = Case(case_id="X-broken", category="clean_control", family=GROUNDING,
                 description="deliberately wrong expectation",
                 runner="exact_decimal", expect="DENY", expect_code="not_a_code",
                 params={"value": "19.99"})
    result = run_case(wrong, capture, as_of=AS_OF)
    assert not result.passed
    assert any("expected DENY" in failure for failure in result.failures)


def test_markdown_names_failures_and_claims():
    report = evaluate(as_of=AS_OF)
    markdown = render_markdown(report)
    assert "Named failures" in markdown
    assert "investment accuracy measured" in markdown
    assert report["dataset_digest"][:23] in markdown
