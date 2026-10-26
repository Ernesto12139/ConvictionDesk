"""The keyless chain must be real, replayable, and honest about what it is not."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from market_agent.run_trace import export_trace, verify_replay
from market_agent.scripted_research import (
    REFUSAL_SCENARIO_ID,
    SCENARIO_ID,
    SYNTHETIC_LABEL,
    exact_decimal,
    run_filing_research,
    run_refused_forward,
    scenario_filing_research,
    synthetic_portfolio,
)

AS_OF = datetime(2026, 10, 26, tzinfo=timezone.utc)
CAPTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "evals" / "fixtures" / "mia_trace_eval" / "GLXY.json")
    .read_text(encoding="utf-8"))


def _run(**overrides):
    params = dict(capture=CAPTURE, as_of=AS_OF, symbol="GLXY", hypothetical_price="24.00")
    params.update(overrides)
    return run_filing_research(**params)


def test_successful_run_executes_real_tools_in_order():
    run = _run()
    assert run.status == "COMPLETE"
    names = [span.name for span in run.spans]
    assert names == ["portfolio.synthetic_snapshot", "APPLICABILITY", "sec.recent_filings",
                     "sec_filings.project", "PIT", "sec.thesis_delta",
                     "scripted_synthesis", "whatif.decimal", "RIGHTS", "BUDGET"]
    sec = next(s for s in run.spans if s.name == "sec.recent_filings")
    assert sec.output_count == len(CAPTURE["rows"])
    assert sec.sources[0].content_hash == CAPTURE["raw_metadata_hash"]
    project = next(s for s in run.spans if s.name == "sec_filings.project")
    assert project.output_count and project.output_count > 0
    thesis = next(s for s in run.spans if s.name == "sec.thesis_delta")
    assert thesis.notes[0].startswith("thesis:TV")
    assert "THESIS_UNCONFIRMED" in thesis.notes[0]


def test_scripted_run_has_zero_model_calls_and_no_cost_line():
    run = _run()
    models = [span.model for span in run.spans if span.model]
    assert len(models) == 1
    assert models[0].execution == "SCRIPTED"
    assert models[0].provider is None
    assert models[0].cost.status == "NOT_APPLICABLE"
    document = export_trace(run)
    assert document["execution_mode"] == "KEYLESS_SCRIPTED"
    assert not any((s.get("model") or {}).get("execution") == "REAL_MODEL"
                   for s in document["spans"])


def test_synthetic_portfolio_is_labelled_and_not_owner_data():
    portfolio = synthetic_portfolio(AS_OF)
    assert portfolio.portfolio_id == "synthetic-demo"
    assert all(position.note == SYNTHETIC_LABEL for position in portfolio.positions)
    assert all(position.account_id.startswith("syn-") for position in portfolio.positions)


def test_unheld_symbol_is_blocked_before_any_source_read():
    run = _run(symbol="AAPL")
    assert run.status == "BLOCKED"
    assert run.error_code == "not_held"
    assert [span.name for span in run.spans] == ["portfolio.synthetic_snapshot", "APPLICABILITY"]
    gate = run.spans[1]
    assert gate.policies[0].decision == "DENY"


def test_invalid_price_is_blocked_and_never_computed():
    run = _run(hypothetical_price="-5")
    assert run.status == "BLOCKED"
    assert run.error_code == "invalid_price"
    whatif = next(s for s in run.spans if s.name == "whatif.decimal")
    assert whatif.status == "ERROR"
    assert whatif.output_count is None


def test_float_price_is_refused_by_the_exact_decimal_guard():
    with pytest.raises(TypeError):
        exact_decimal(24.0)
    assert exact_decimal("24.00") == exact_decimal("24")


def test_pit_before_filing_yields_no_eligible_event():
    run = _run(as_of=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert run.status == "BLOCKED"
    assert run.error_code == "no_eligible_filing"
    project = next(s for s in run.spans if s.name == "sec_filings.project")
    assert project.output_count == 0


def test_refusal_trace_uses_the_production_consent_gate():
    run = run_refused_forward()
    assert run.status == "BLOCKED"
    assert run.error_code == "egress_confirmation_required"
    assert run.error_category == "CONSENT_REQUIRED"
    assert run.scenario_id == REFUSAL_SCENARIO_ID
    attempt, decision = run.spans
    assert attempt.status == "BLOCKED"
    assert decision.policies[0].decision == "DENY"
    assert decision.policies[0].gate == "CONSENT"


def test_replay_reproduces_the_same_outcome_digest():
    first = scenario_filing_research(CAPTURE, as_of=AS_OF, symbol="GLXY",
                                     hypothetical_price="24.00")
    document = export_trace(first)
    outcome = verify_replay(document, lambda: scenario_filing_research(
        CAPTURE, as_of=AS_OF, symbol="GLXY", hypothetical_price="24.00"))
    assert outcome.status == "REPLAY_OK"


def test_changing_only_the_hypothetical_price_keeps_the_same_event():
    cheap = _run(hypothetical_price="10.00")
    rich = _run(hypothetical_price="40.00")
    assert cheap.status == rich.status == "COMPLETE"
    thesis_cheap = next(s for s in cheap.spans if s.name == "sec.thesis_delta")
    thesis_rich = next(s for s in rich.spans if s.name == "sec.thesis_delta")
    assert thesis_cheap.notes == thesis_rich.notes
    assert cheap.outcome_digest == rich.outcome_digest


def test_exported_trace_never_contains_synthetic_quantities_in_inputs():
    document = export_trace(_run())
    for span in document["spans"]:
        assert "quantity" not in (span.get("input_class") or {})
        assert "cost_basis" not in (span.get("input_class") or {})
