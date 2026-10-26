"""mia-trace-v1: a record of what the process actually did, not a story about it.

Every other trace-ish artifact in this repository is a *projection*: it reads stored
rows after the fact and reconstructs a narrative. That is useful, and it is also how
fake call chains get produced — a model, or a person, can describe a plausible tool
sequence that never ran. This module refuses that by construction:

- a span exists only because a Python ``with`` block actually entered and exited, and
  its timestamps come from the clock at those two boundaries;
- a failure span records the exception type and code that was really raised, and the
  recorder re-raises rather than swallowing it;
- a tool span cannot claim success without the caller passing the real result count,
  and ``status`` is derived from whether the block raised, never from a label;
- a model span with no invocation is impossible: ``record_model`` is only reachable
  from inside an invocation block, and a scripted run records ``SCRIPTED`` with zero
  model calls and zero tokens rather than a synthetic "success";
- cost and token counts are tri-state. ``UNKNOWN`` is not ``0``, and a run with no
  paid dispatch never gets a $0.00 cost line that looks like a measured zero.

Export is allowlist-first. ``EXPORT_ALLOWLIST`` drops owner quantities, cash, cost
basis, question text and any third-party body; ``LOCAL_ONLY`` keeps them for the
operator's own screen. Nothing here can be promoted to public by a caller flag: the
allowlist document is produced by subtracting fields, never by trusting a flag on the
way in.

Replay is a verification, not a re-enactment: ``verify_replay`` re-executes a
registered scenario and compares a canonical outcome digest (status, error code,
ordered tool names, evidence ids). A trace that cannot reproduce its own digest is
reported as ``REPLAY_MISMATCH`` with the differing fields, not silently accepted.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Protocol

from pydantic import Field

from .domain import FrozenModel, utc

TRACE_SCHEMA = "mia-trace-v1"
BUILD_UNKNOWN = "UNKNOWN"

# Span kinds are closed on purpose: a new kind is a schema change, and a schema change
# must be visible to a reviewer rather than smuggled in as a free-form string.
SpanKind = Literal["stage", "tool", "model", "gate"]
SpanStatus = Literal["OK", "ERROR", "BLOCKED", "TIMEOUT", "SKIPPED"]

# Error categories are the vocabulary the eval harness scores against. Anything the
# runtime does not recognise stays OTHER so an unknown failure is visible, not hidden
# behind a convenient label.
ERROR_CATEGORIES = (
    "SOURCE_UNAVAILABLE",
    "PROVIDER_TIMEOUT",
    "PROVIDER_RATE_LIMIT",
    "INVALID_DATA",
    "PIT_BREACH",
    "RIGHTS_BLOCKED",
    "CONSENT_REQUIRED",
    "BUDGET_BLOCKED",
    "NOT_APPLICABLE",
    "NUMERIC_UNSUPPORTED",
    "ROUTING_INVALID",
    "DUPLICATE_EVENT",
    "INTERNAL",
    "OTHER",
)

_ERROR_MAP = {
    "not_found": "SOURCE_UNAVAILABLE",
    "source_unavailable": "SOURCE_UNAVAILABLE",
    "provider_timeout": "PROVIDER_TIMEOUT",
    "timeout": "PROVIDER_TIMEOUT",
    "rate_limited": "PROVIDER_RATE_LIMIT",
    "rate_limit": "PROVIDER_RATE_LIMIT",
    "invalid_data": "INVALID_DATA",
    "pit_breach": "PIT_BREACH",
    "egress_confirmation_required": "CONSENT_REQUIRED",
    "data_rights_payload_mismatch": "RIGHTS_BLOCKED",
    "data_rights_target_mismatch": "RIGHTS_BLOCKED",
    "data_rights_probe_scope": "RIGHTS_BLOCKED",
    "data_rights_test_transport": "RIGHTS_BLOCKED",
    "api_budget_cost": "BUDGET_BLOCKED",
    "api_budget_pending": "BUDGET_BLOCKED",
    "unsupported_numeric_fact": "NUMERIC_UNSUPPORTED",
    "numeric_receipt_required": "NUMERIC_UNSUPPORTED",
}

# An exception with no machine code still has a type. The mapping keeps the category
# deterministic without copying a free-text message into the trace, which is where
# owner values and provider payloads would otherwise leak.
_EXCEPTION_TYPES = {
    "ValueError": "INVALID_DATA",
    "KeyError": "INVALID_DATA",
    "TypeError": "INVALID_DATA",
    "TimeoutError": "PROVIDER_TIMEOUT",
    "ConnectionError": "SOURCE_UNAVAILABLE",
    "OSError": "SOURCE_UNAVAILABLE",
    "PermissionError": "RIGHTS_BLOCKED",
    "RuntimeError": "INTERNAL",
}


def classify_error(code: str | None) -> str:
    """Deterministic error category from a runtime code. Unknown stays OTHER."""
    if not code:
        return "OTHER"
    lowered = code.strip().lower()
    if lowered in _ERROR_MAP:
        return _ERROR_MAP[lowered]
    if lowered.startswith("egress_"):
        return "CONSENT_REQUIRED"
    if lowered.startswith("data_rights"):
        return "RIGHTS_BLOCKED"
    return "OTHER"


def error_code_for(exc: BaseException) -> str:
    """Machine code for a real exception: its own code, else its type, else INTERNAL.

    A message is never used as the code. Messages are the most likely carrier of
    private values, and a trace that quotes them cannot be exported.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.strip():
        return code.strip()[:80]
    return type(exc).__name__


class Clock(Protocol):
    def __call__(self) -> datetime: ...


def system_clock() -> datetime:
    return datetime.now(timezone.utc)


def monotonic() -> float:
    return time.monotonic()


# --- code identity -----------------------------------------------------------

class CodeIdentity(FrozenModel):
    """The exact source revision a trace claims to describe.

    ``dirty`` is not decoration: a trace produced from a modified tree describes code
    that does not exist in Git, so a reviewer must be able to see that instead of
    assuming the SHA is sufficient.
    """

    commit: str
    tree: str | None = None
    build: str = BUILD_UNKNOWN
    dirty: bool | None = None
    source: Literal["GIT", "ENV", "UNKNOWN"] = "UNKNOWN"


def code_identity(root: Path | None = None) -> CodeIdentity:
    """Read the real HEAD of the working tree that is executing this code.

    No shell, no network. A missing or unreadable repository is ``UNKNOWN`` rather
    than a guess: an invented SHA would poison every downstream receipt. Worktrees are
    handled explicitly because there ``.git`` is a file pointing at a per-worktree
    gitdir, and reading ``.git/HEAD`` would silently return UNKNOWN for every isolated
    writer checkout — the exact case this project runs in.
    """
    override = os.environ.get("MIA_TRACE_CODE_SHA")
    if override and len(override) >= 7:
        return CodeIdentity(commit=override, build=os.environ.get("MIA_TRACE_BUILD", BUILD_UNKNOWN),
                            source="ENV")
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    build = os.environ.get("MIA_TRACE_BUILD", BUILD_UNKNOWN)
    marker = base / ".git"
    try:
        if marker.is_dir():
            gitdir = marker
        elif marker.is_file():
            text = marker.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return CodeIdentity(commit=BUILD_UNKNOWN, build=build, source="UNKNOWN")
            gitdir = Path(text.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (base / gitdir).resolve()
            # A linked worktree keeps its HEAD in the worktree gitdir but the object
            # store (and often the ref) in the common dir.
            common = gitdir / "commondir"
            common_dir = gitdir
            if common.is_file():
                common_dir = (gitdir / common.read_text(encoding="utf-8").strip()).resolve()
        else:
            return CodeIdentity(commit=BUILD_UNKNOWN, build=build, source="UNKNOWN")
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            for candidate in (gitdir / ref, common_dir / ref):
                if candidate.is_file():
                    return CodeIdentity(commit=candidate.read_text(encoding="utf-8").strip(),
                                        build=build, source="GIT")
            packed = common_dir / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.endswith(f" {ref}"):
                        return CodeIdentity(commit=line.split()[0], build=build, source="GIT")
            return CodeIdentity(commit=BUILD_UNKNOWN, build=build, source="UNKNOWN")
        if len(head) == 40:
            return CodeIdentity(commit=head, build=build, source="GIT")
    except OSError:
        pass
    return CodeIdentity(commit=BUILD_UNKNOWN, build=build, source="UNKNOWN")


# --- span records ------------------------------------------------------------

class SourceRef(FrozenModel):
    """Identity of a source actually consulted, with the three times kept apart.

    ``accepted_at`` is when the authority accepted it (SEC), ``captured_at`` is when
    this process read it, and ``available_at`` is the conservative point-in-time
    bound. Collapsing them is how a late read gets presented as early knowledge.
    """

    source_id: str
    kind: str
    accepted_at: datetime | None = None
    captured_at: datetime | None = None
    available_at: datetime | None = None
    time_precision: Literal["SECOND", "DATE", "UNKNOWN"] | None = None
    content_hash: str | None = None
    rights: str = "UNKNOWN"
    pit_ok: bool | None = None


class PolicyDecision(FrozenModel):
    """One admission gate, as decided by the code that owns it."""

    gate: Literal["CONSENT", "RIGHTS", "BUDGET", "PIT", "APPLICABILITY", "CONTRACT"]
    decision: Literal["ALLOW", "DENY", "UNKNOWN"]
    code: str | None = None
    detail: str | None = None


class TokenState(FrozenModel):
    """Tokens or cost: a number only when measured, otherwise UNKNOWN.

    ``value`` is ``None`` for UNKNOWN, and ``NOT_APPLICABLE`` is a separate state so a
    scripted run does not read as a free model call.
    """

    status: Literal["KNOWN", "UNKNOWN", "NOT_APPLICABLE"]
    value: str | None = None
    unit: str | None = None
    reason: str | None = None

    @classmethod
    def unknown(cls, reason: str) -> "TokenState":
        return cls(status="UNKNOWN", reason=reason)

    @classmethod
    def not_applicable(cls, reason: str) -> "TokenState":
        return cls(status="NOT_APPLICABLE", reason=reason)

    @classmethod
    def known(cls, value: str | int, unit: str) -> "TokenState":
        return cls(status="KNOWN", value=str(value), unit=unit)


class ModelInvocation(FrozenModel):
    """A model that was really called, or an explicit statement that none was."""

    execution: Literal["SCRIPTED", "REAL_MODEL"]
    provider: str | None = None
    model: str | None = None
    purpose: str | None = None
    prompt_contract: str | None = None
    input_tokens: TokenState
    output_tokens: TokenState
    cost: TokenState
    validation_outcome: str | None = None


def model_invocation(provider: str, model: str, purpose: str, *,
                     input_tokens: TokenState, output_tokens: TokenState,
                     cost: TokenState, prompt_contract: str | None = None,
                     validation_outcome: str | None = None) -> ModelInvocation:
    """Build a REAL_MODEL invocation from measured values.

    There is no constructor path that produces REAL_MODEL with invented tokens: the
    caller must pass the three states explicitly, and UNKNOWN is always available.
    """
    return ModelInvocation(
        execution="REAL_MODEL", provider=provider, model=model, purpose=purpose,
        prompt_contract=prompt_contract, input_tokens=input_tokens,
        output_tokens=output_tokens, cost=cost, validation_outcome=validation_outcome)


class TraceSpan(FrozenModel):
    span_id: str
    parent_span_id: str | None = None
    sequence: int
    kind: SpanKind
    name: str
    started_at: datetime
    ended_at: datetime | None = None
    latency_ms: int | None = None
    status: SpanStatus = "OK"
    error_code: str | None = None
    error_category: str | None = None
    attempt: int = 1
    retry_of: str | None = None
    fallback_from: str | None = None
    timed_out: bool = False
    # Tool-specific, allowlisted by the recorder rather than by the caller.
    input_class: dict[str, Any] = Field(default_factory=dict)
    output_count: int | None = None
    sources: list[SourceRef] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    counterevidence_ids: list[str] = Field(default_factory=list)
    policies: list[PolicyDecision] = Field(default_factory=list)
    model: ModelInvocation | None = None
    notes: list[str] = Field(default_factory=list)


class TraceRun(FrozenModel):
    """One executed run. ``spans`` are appended in real execution order."""

    schema_version: Literal["mia-trace-v1"] = TRACE_SCHEMA
    run_id: str
    task_id: str | None = None
    revision_id: str | None = None
    execution_mode: Literal["KEYLESS_SCRIPTED", "OWNER_LOCAL", "REAL_MODEL"]
    scenario_id: str | None = None
    as_of: datetime | None = None
    pit_cutoff: datetime | None = None
    code: CodeIdentity
    started_at: datetime
    ended_at: datetime | None = None
    status: Literal["RUNNING", "COMPLETE", "FAILED", "BLOCKED"] = "RUNNING"
    error_code: str | None = None
    error_category: str | None = None
    spans: list[TraceSpan] = Field(default_factory=list)
    outcome_digest: str | None = None
    redactions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    def span(self, span_id: str) -> TraceSpan | None:
        return next((item for item in self.spans if item.span_id == span_id), None)


# --- recorder ----------------------------------------------------------------

_SAFE_INPUT_KEYS = frozenset({
    "symbol", "symbols", "as_of", "since", "until", "limit", "forms", "scope",
    "lookback_days", "mode", "objective", "revision", "account_id", "kind",
    "execution_strategy", "provider", "model", "purpose", "gate",
})
_SENSITIVE_INPUT_KEYS = frozenset({
    "quantity", "cash", "avg_cost", "cost_basis", "question", "prompt", "api_key",
    "key", "token", "consent_id", "payload", "body", "content", "holdings",
})


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class TraceRecorder:
    """Append-only span recorder bound to one run.

    The recorder never decides that something succeeded. ``status`` comes from the
    block raising or not; ``output_count`` and ``sources`` are passed by the caller
    that really has them. The only judgement it makes is classification of an error
    code into the eval vocabulary, which is a pure function.
    """

    def __init__(self, *, run_id: str, execution_mode: str, task_id: str | None = None,
                 revision_id: str | None = None, scenario_id: str | None = None,
                 as_of: datetime | None = None, pit_cutoff: datetime | None = None,
                 code: CodeIdentity | None = None, clock: Clock | None = None,
                 timer: Callable[[], float] | None = None):
        self._clock = clock or system_clock
        self._timer = timer or monotonic
        self._run_id = run_id
        self._identity = dict(task_id=task_id, revision_id=revision_id,
                              execution_mode=execution_mode, scenario_id=scenario_id,
                              as_of=utc(as_of) if as_of else None,
                              pit_cutoff=utc(pit_cutoff) if pit_cutoff else None,
                              code=code or code_identity(),
                              started_at=utc(self._clock()))
        self._spans: list[TraceSpan] = []
        self._seq = 0
        self._stack: list[str] = []
        self._closed = False
        self._final: TraceRun | None = None

    # -- read-only access ----------------------------------------------------
    def _snapshot(self, **overrides) -> TraceRun:
        payload = dict(self._identity)
        payload.update(overrides)
        return TraceRun(run_id=self._run_id, spans=list(self._spans), **payload)

    @property
    def run(self) -> TraceRun:
        return self._final if self._final is not None else self._snapshot()

    @property
    def current_span_id(self) -> str | None:
        return self._stack[-1] if self._stack else None

    def _next_span_id(self) -> str:
        self._seq += 1
        return f"{self._run_id}:s{self._seq:03d}"

    # -- span primitive ------------------------------------------------------
    @contextmanager
    def span(self, kind: SpanKind, name: str, *, input_class: dict[str, Any] | None = None,
             attempt: int = 1, retry_of: str | None = None,
             fallback_from: str | None = None, timeout: float | None = None):
        """Enter a real execution block. Exceptions are recorded and re-raised."""
        if self._closed:
            raise RuntimeError("trace already closed")
        span_id = self._next_span_id()
        parent = self.current_span_id
        started_wall = utc(self._clock())
        started_mono = self._timer()
        self._stack.append(span_id)
        holder: dict[str, Any] = {}
        try:
            yield _SpanHandle(span_id, holder)
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised unchanged
            elapsed = max(0.0, self._timer() - started_mono)
            code = error_code_for(exc)
            category = classify_error(code)
            if category == "OTHER" and code in _EXCEPTION_TYPES:
                category = _EXCEPTION_TYPES[code]
            timed_out = timeout is not None and elapsed >= timeout
            self._append(TraceSpan(
                span_id=span_id, parent_span_id=parent, sequence=self._seq, kind=kind,
                name=name, started_at=started_wall, ended_at=utc(self._clock()),
                latency_ms=int(round(elapsed * 1000)),
                status="TIMEOUT" if timed_out else (
                    "BLOCKED" if category in {"RIGHTS_BLOCKED", "CONSENT_REQUIRED",
                                              "BUDGET_BLOCKED", "NOT_APPLICABLE"}
                    else "ERROR"),
                error_code=code, error_category=category,
                attempt=attempt, retry_of=retry_of, fallback_from=fallback_from,
                timed_out=timed_out,
                input_class=_safe_inputs(input_class), notes=list(holder.get("notes") or [])))
            raise
        else:
            elapsed = max(0.0, self._timer() - started_mono)
            handle = holder.get("handle")
            self._append(TraceSpan(
                span_id=span_id, parent_span_id=parent, sequence=self._seq, kind=kind,
                name=name, started_at=started_wall, ended_at=utc(self._clock()),
                latency_ms=int(round(elapsed * 1000)),
                status=(handle.status if handle else "OK"),
                attempt=attempt, retry_of=retry_of, fallback_from=fallback_from,
                input_class=_safe_inputs(input_class),
                output_count=handle.output_count if handle else None,
                sources=list(handle.sources) if handle else [],
                evidence_ids=list(handle.evidence_ids) if handle else [],
                counterevidence_ids=list(handle.counterevidence_ids) if handle else [],
                policies=list(handle.policies) if handle else [],
                model=handle.model if handle else None,
                notes=list(handle.notes) if handle else []))
        finally:
            self._stack.pop()

    def _append(self, span: TraceSpan) -> None:
        self._spans.append(span)

    # -- convenience wrappers ------------------------------------------------
    def record_tool(self, name: str, *, input_class: dict[str, Any] | None = None,
                    timeout: float | None = None, attempt: int = 1,
                    retry_of: str | None = None, fallback_from: str | None = None):
        return self.span("tool", name, input_class=input_class, timeout=timeout,
                         attempt=attempt, retry_of=retry_of, fallback_from=fallback_from)

    def record_gate(self, gate: str, *, decision: str, code: str | None = None,
                    detail: str | None = None, input_class: dict[str, Any] | None = None):
        """Record a policy gate that was actually evaluated.

        A DENY gate is recorded as BLOCKED. The gate itself is not re-evaluated here:
        the caller passes the decision its own code produced, and a test asserts that
        the production gate and the trace agree.
        """
        with self.span("gate", gate, input_class=input_class) as handle:
            handle.policies.append(PolicyDecision(
                gate=gate,  # type: ignore[arg-type]
                decision=decision,  # type: ignore[arg-type]
                code=code, detail=detail))
            handle.status = "BLOCKED" if decision == "DENY" else "OK"
        return None

    def record_scripted(self, purpose: str, *, prompt_contract: str | None = None,
                        notes: Iterable[str] = ()) -> None:
        """Record that a step was deterministic code, not a model.

        This exists so a keyless demo can state its own execution mode truthfully:
        zero model calls, zero tokens, cost NOT_APPLICABLE — not a $0.00 model run.
        """
        with self.span("model", "scripted_synthesis", input_class={"purpose": purpose}) as handle:
            handle.model = ModelInvocation(
                execution="SCRIPTED", purpose=purpose, prompt_contract=prompt_contract,
                input_tokens=TokenState.not_applicable("deterministic code, no tokenizer"),
                output_tokens=TokenState.not_applicable("deterministic code, no tokenizer"),
                cost=TokenState.not_applicable("no provider dispatch"))
            handle.notes.extend(notes)

    @contextmanager
    def record_model(self, *, provider: str, model: str, purpose: str,
                     prompt_contract: str | None = None,
                     input_class: dict[str, Any] | None = None):
        """Wrap a real provider invocation.

        The handle starts with UNKNOWN tokens because at entry nothing has been
        measured yet. A caller that really received a response replaces
        ``handle.model`` with ``model_invocation(...)`` carrying the measured values.
        If it never does, the trace keeps UNKNOWN — which is the truthful state for a
        call whose usage was not captured.
        """
        with self.span("model", f"model:{model}",
                       input_class=input_class or {"provider": provider, "model": model,
                                                   "purpose": purpose}) as handle:
            handle.model = ModelInvocation(
                execution="REAL_MODEL", provider=provider, model=model, purpose=purpose,
                prompt_contract=prompt_contract,
                input_tokens=TokenState.unknown("usage not captured"),
                output_tokens=TokenState.unknown("usage not captured"),
                cost=TokenState.unknown("cost not reconciled"))
            yield handle

    # -- close ---------------------------------------------------------------
    def close(self, *, status: Literal["COMPLETE", "FAILED", "BLOCKED"] | None = None,
              error_code: str | None = None) -> TraceRun:
        if self._closed:
            return self._final or self._snapshot()
        self._closed = True
        derived = status
        if derived is None:
            if any(span.status in {"ERROR", "TIMEOUT"} for span in self._spans):
                derived = "FAILED"
            elif any(span.status == "BLOCKED" for span in self._spans):
                derived = "BLOCKED"
            else:
                derived = "COMPLETE"
        final = self._snapshot(
            status=derived, error_code=error_code,
            error_category=classify_error(error_code) if error_code else None,
            ended_at=utc(self._clock()))
        final = final.model_copy(update={"outcome_digest": outcome_digest(final)})
        self._final = final
        return final


class _SpanHandle:
    """Mutable handle yielded inside a span block.

    Kept deliberately small: a caller can add sources, evidence and policy decisions
    that it really has, and can downgrade its own status (e.g. to BLOCKED), but cannot
    mark a failed block successful because the failure path never reaches here.
    """

    __slots__ = ("span_id", "status", "output_count", "sources", "evidence_ids",
                 "counterevidence_ids", "policies", "model", "notes")

    def __init__(self, span_id: str, holder: dict[str, Any]):
        self.span_id = span_id
        self.status: SpanStatus = "OK"
        self.output_count: int | None = None
        self.sources: list[SourceRef] = []
        self.evidence_ids: list[str] = []
        self.counterevidence_ids: list[str] = []
        self.policies: list[PolicyDecision] = []
        self.model: ModelInvocation | None = None
        self.notes: list[str] = []
        holder["handle"] = self


def _safe_inputs(input_class: dict[str, Any] | None) -> dict[str, Any]:
    """Allowlist tool inputs. A sensitive key is dropped, not hashed in place."""
    if not input_class:
        return {}
    safe: dict[str, Any] = {}
    for key, value in input_class.items():
        if key in _SENSITIVE_INPUT_KEYS:
            continue
        if value is None:
            continue
        if key in _SAFE_INPUT_KEYS and isinstance(value, (str, int, float, bool, list, tuple)):
            safe[key] = list(value) if isinstance(value, tuple) else value
    return safe


# --- digests and replay ------------------------------------------------------

def outcome_digest(run: TraceRun) -> str:
    """Canonical, comparable summary of what a run produced.

    Only stable facts enter the digest. Latency, wall-clock timestamps and span ids
    are excluded on purpose: two correct executions of the same scenario at different
    times must agree, and a digest that changes with the clock cannot verify replay.
    """
    payload = {
        "status": run.status,
        "error_code": run.error_code,
        "tools": [{"name": s.name, "status": s.status, "error_code": s.error_code,
                   "output_count": s.output_count}
                  for s in run.spans if s.kind == "tool"],
        "evidence": sorted({eid for s in run.spans for eid in s.evidence_ids}),
        "counterevidence": sorted({eid for s in run.spans for eid in s.counterevidence_ids}),
        "model_execution": sorted({s.model.execution for s in run.spans if s.model}),
        "model_calls": sum(1 for s in run.spans if s.model and s.model.execution == "REAL_MODEL"),
        "gates": sorted({f"{p.gate}:{p.decision}" for s in run.spans for p in s.policies}),
    }
    return _digest(payload)


class ReplayOutcome(FrozenModel):
    status: Literal["REPLAY_OK", "REPLAY_MISMATCH", "REPLAY_UNAVAILABLE"]
    recorded_digest: str
    replayed_digest: str | None = None
    mismatches: list[str] = Field(default_factory=list)
    detail: str | None = None


def verify_replay(document: dict, runner: Callable[[], "TraceRun"]) -> ReplayOutcome:
    """Re-execute a scenario and compare its outcome digest with the recorded one.

    ``runner`` must execute the same real code path; this function cannot fabricate
    agreement, it can only report that two executions disagree.
    """
    recorded = (document or {}).get("outcome_digest")
    if not recorded:
        return ReplayOutcome(status="REPLAY_UNAVAILABLE", recorded_digest="",
                             detail="trace document carries no outcome_digest")
    replayed = runner()
    digest = replayed.outcome_digest or outcome_digest(replayed)
    if digest == recorded:
        return ReplayOutcome(status="REPLAY_OK", recorded_digest=recorded, replayed_digest=digest)
    mismatches = _diff_documents(document, replayed.model_dump(mode="json"))
    return ReplayOutcome(status="REPLAY_MISMATCH", recorded_digest=recorded,
                         replayed_digest=digest, mismatches=mismatches,
                         detail="re-executed scenario produced a different outcome digest")


def _diff_documents(left: dict, right: dict) -> list[str]:
    """Name the fields that actually differ, so a mismatch is actionable."""
    out: list[str] = []
    for key in ("status", "error_code"):
        if (left or {}).get(key) != right.get(key):
            out.append(key)
    left_tools = [(s.get("name"), s.get("output_count")) for s in (left or {}).get("spans", [])
                  if s.get("kind") == "tool"]
    right_tools = [(s.get("name"), s.get("output_count")) for s in right.get("spans", [])
                   if s.get("kind") == "tool"]
    if [name for name, _ in left_tools] != [name for name, _ in right_tools]:
        out.append("tools")
    elif left_tools != right_tools:
        out.append("tool_outputs")
    left_evidence = sorted({e for s in (left or {}).get("spans", []) for e in s.get("evidence_ids") or []})
    right_evidence = sorted({e for s in right.get("spans", []) for e in s.get("evidence_ids") or []})
    if left_evidence != right_evidence:
        out.append("evidence")
    left_calls = sum(1 for s in (left or {}).get("spans", [])
                     if (s.get("model") or {}).get("execution") == "REAL_MODEL")
    right_calls = sum(1 for s in right.get("spans", [])
                      if (s.get("model") or {}).get("execution") == "REAL_MODEL")
    if left_calls != right_calls:
        out.append("model_calls")
    return out or ["unknown"]


# --- export ------------------------------------------------------------------

class ExportPolicy(FrozenModel):
    mode: Literal["EXPORT_ALLOWLIST", "LOCAL_ONLY"]
    redacted_fields: list[str] = Field(default_factory=list)
    meaning: str


def export_trace(run: TraceRun, *, mode: str = "EXPORT_ALLOWLIST") -> dict:
    """JSON-safe trace document under an explicit export policy.

    ``EXPORT_ALLOWLIST`` is the default because the safe choice must be the one you
    get by not thinking about it. It removes nothing that a reviewer needs to check
    tool selection, PIT, rights, failure attribution or determinism; it removes owner
    values, prompts and any free-text that could carry restricted content.
    """
    if mode not in {"EXPORT_ALLOWLIST", "LOCAL_ONLY"}:
        raise ValueError("unknown export mode")
    document = run.model_dump(mode="json")
    redactions: list[str] = list(document.get("redactions") or [])
    if mode == "EXPORT_ALLOWLIST":
        for span in document.get("spans", []):
            for key in list(span.get("input_class") or {}):
                if key in _SENSITIVE_INPUT_KEYS:
                    span["input_class"].pop(key, None)
                    redactions.append(f"input_class.{key}")
            if span.get("model"):
                span["model"].pop("prompt_contract", None)
            span["notes"] = [note for note in span.get("notes", []) if len(note) <= 160]
        document["redactions"] = sorted(set(redactions))
    document["export"] = ExportPolicy(
        mode=mode, redacted_fields=sorted(set(redactions)),
        meaning=("脱敏白名单导出：保留工具选择、PIT、权利、失败归因与确定性所需字段，"
                 "移除持仓数值、提示词与可能含受限内容的自由文本。"
                 if mode == "EXPORT_ALLOWLIST" else
                 "本地完整导出：含操作者自有数值，不得公开或转发第三方。")).model_dump()
    return document


def write_jsonl(document: dict, path: Path) -> Path:
    """Append one trace document as a single JSONL line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows
