"""Trace runtime: the recorder must describe what ran, and refuse to flatter it."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from market_agent.llm import LLMError
from market_agent.run_trace import (
    CodeIdentity,
    SourceRef,
    TokenState,
    TraceRecorder,
    classify_error,
    code_identity,
    error_code_for,
    export_trace,
    model_invocation,
    outcome_digest,
    read_jsonl,
    verify_replay,
    write_jsonl,
)

AS_OF = datetime(2026, 10, 26, tzinfo=timezone.utc)


def _recorder(run_id="trace-test", mode="KEYLESS_SCRIPTED"):
    return TraceRecorder(run_id=run_id, execution_mode=mode, scenario_id="unit",
                         as_of=AS_OF, pit_cutoff=AS_OF,
                         code=CodeIdentity(commit="0" * 40, build="test", source="ENV"))


def test_span_records_real_boundaries_and_latency():
    ticks = iter([10.0, 10.25])
    recorder = TraceRecorder(run_id="t", execution_mode="KEYLESS_SCRIPTED",
                             clock=lambda: AS_OF, timer=lambda: next(ticks),
                             code=CodeIdentity(commit="0" * 40, source="ENV"))
    with recorder.record_tool("sec.recent_filings", input_class={"symbol": "GLXY"}) as span:
        span.output_count = 3
    run = recorder.close()
    assert run.status == "COMPLETE"
    assert run.spans[0].latency_ms == 250
    assert run.spans[0].status == "OK"
    assert run.spans[0].output_count == 3


def test_exception_is_recorded_and_reraised():
    recorder = _recorder()
    with pytest.raises(ValueError):
        with recorder.record_tool("sec.bad", input_class={"symbol": "GLXY"}):
            raise ValueError("invalid_data: owner holds 28 shares")
    run = recorder.close()
    span = run.spans[0]
    assert span.status == "ERROR"
    assert span.error_category == "INVALID_DATA"
    # The message is never copied into the trace: it is the most likely leak carrier.
    assert "28 shares" not in json.dumps(run.model_dump(mode="json"))
    assert run.status == "FAILED"


def test_rights_failure_is_blocked_not_error():
    recorder = _recorder()
    with pytest.raises(LLMError):
        with recorder.record_tool("model.forward"):
            raise LLMError("egress_confirmation_required", "no receipt")
    run = recorder.close()
    assert run.spans[0].status == "BLOCKED"
    assert run.spans[0].error_category == "CONSENT_REQUIRED"
    assert run.status == "BLOCKED"


def test_sensitive_inputs_are_dropped_not_hashed():
    recorder = _recorder()
    with recorder.record_tool("portfolio.snapshot",
                              input_class={"symbol": "GLXY", "quantity": "28",
                                           "cash": "300", "question": "should I sell?"}) as span:
        span.output_count = 2
    span = recorder.close().spans[0]
    assert span.input_class == {"symbol": "GLXY"}
    assert "28" not in json.dumps(span.model_dump(mode="json"))


def test_scripted_step_is_not_a_zero_cost_model_call():
    recorder = _recorder()
    recorder.record_scripted("research_summary", prompt_contract="template-v1")
    run = recorder.close()
    model = run.spans[0].model
    assert model is not None and model.execution == "SCRIPTED"
    assert model.input_tokens.status == "NOT_APPLICABLE"
    assert model.cost.status == "NOT_APPLICABLE"
    assert outcome_digest(run)  # digest exists without any provider call


def test_real_model_without_measured_usage_stays_unknown():
    recorder = _recorder()
    with recorder.record_model(provider="openai", model="gpt-demo", purpose="synthesis"):
        pass
    model = recorder.close().spans[0].model
    assert model.execution == "REAL_MODEL"
    assert model.input_tokens.status == "UNKNOWN"
    assert model.cost.value is None
    measured = model_invocation("openai", "gpt-demo", "synthesis",
                                input_tokens=TokenState.known(120, "tokens"),
                                output_tokens=TokenState.known(40, "tokens"),
                                cost=TokenState.known("0.0021", "USD"))
    assert measured.input_tokens.value == "120"
    assert measured.cost.value == "0.0021"


def test_digest_ignores_clock_but_catches_real_differences():
    first = _recorder("a")
    with first.record_tool("sec.recent_filings", input_class={"symbol": "GLXY"}) as span:
        span.output_count = 3
    run_a = first.close()

    ticks = iter([0.0, 5.0])
    second = TraceRecorder(run_id="b", execution_mode="KEYLESS_SCRIPTED", as_of=AS_OF,
                           clock=lambda: AS_OF, timer=lambda: next(ticks),
                           code=CodeIdentity(commit="0" * 40, source="ENV"))
    with second.record_tool("sec.recent_filings", input_class={"symbol": "GLXY"}) as span:
        span.output_count = 3
    run_b = second.close()
    assert run_a.spans[0].latency_ms != run_b.spans[0].latency_ms
    assert run_a.outcome_digest == run_b.outcome_digest

    third = _recorder("c")
    with third.record_tool("sec.recent_filings", input_class={"symbol": "GLXY"}) as span:
        span.output_count = 9
    assert third.close().outcome_digest != run_a.outcome_digest


def test_export_allowlist_removes_prompt_and_keeps_review_fields():
    recorder = _recorder()
    with recorder.record_tool("sec.recent_filings", input_class={"symbol": "GLXY"}) as span:
        span.sources.append(SourceRef(source_id="0001859392-26-000105", kind="SEC_EDGAR",
                                      content_hash="sha256:" + "1" * 64,
                                      rights="PUBLIC_FILING_METADATA"))
        span.evidence_ids = ["Eabc"]
    with recorder.record_model(provider="openai", model="gpt-demo", purpose="synthesis",
                               prompt_contract="secret-prompt-v1"):
        pass
    run = recorder.close()
    exported = export_trace(run)
    assert exported["export"]["mode"] == "EXPORT_ALLOWLIST"
    assert exported["spans"][1]["model"].get("prompt_contract") is None
    assert exported["spans"][0]["sources"][0]["source_id"] == "0001859392-26-000105"
    assert exported["spans"][0]["evidence_ids"] == ["Eabc"]
    local = export_trace(run, mode="LOCAL_ONLY")
    assert local["export"]["mode"] == "LOCAL_ONLY"


def test_replay_reports_ok_and_mismatch():
    def build(output_count):
        recorder = _recorder("replay")
        with recorder.record_tool("sec.recent_filings") as span:
            span.output_count = output_count
        return recorder.close()

    recorded = build(3)
    document = export_trace(recorded)
    assert verify_replay(document, lambda: build(3)).status == "REPLAY_OK"
    mismatch = verify_replay(document, lambda: build(4))
    assert mismatch.status == "REPLAY_MISMATCH"
    assert "tool_outputs" in mismatch.mismatches
    assert verify_replay({}, lambda: build(3)).status == "REPLAY_UNAVAILABLE"


def test_error_classification_is_closed_and_deterministic():
    assert classify_error("egress_confirmation_required") == "CONSENT_REQUIRED"
    assert classify_error("data_rights_payload_mismatch") == "RIGHTS_BLOCKED"
    assert classify_error("api_budget_cost") == "BUDGET_BLOCKED"
    assert classify_error("something_new") == "OTHER"
    assert error_code_for(ValueError("owner holds 28 shares")) == "ValueError"
    assert error_code_for(LLMError("egress_confirmation_required", "x")) == "egress_confirmation_required"


def test_code_identity_env_override_and_unknown(tmp_path):
    identity = code_identity(tmp_path)
    assert identity.source == "UNKNOWN"
    assert identity.commit == "UNKNOWN"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("MIA_TRACE_CODE_SHA", "a" * 40)
    try:
        assert code_identity(tmp_path).commit == "a" * 40
    finally:
        monkeypatch.undo()


def test_jsonl_roundtrip(tmp_path):
    recorder = _recorder()
    with recorder.record_tool("sec.recent_filings") as span:
        span.output_count = 1
    document = export_trace(recorder.close())
    path = write_jsonl(document, tmp_path / "traces" / "run.jsonl")
    rows = read_jsonl(path)
    assert len(rows) == 1
    assert rows[0]["run_id"] == "trace-test"
    assert rows[0]["schema_version"] == "mia-trace-v1"


def test_closed_recorder_refuses_new_spans():
    recorder = _recorder()
    recorder.close()
    with pytest.raises(RuntimeError):
        with recorder.record_tool("late"):
            pass
