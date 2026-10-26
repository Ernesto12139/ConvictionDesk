"""Generate, inspect and replay real traces without a web platform.

    python scripts/mia_trace.py demo --out output/mia-trace
    python scripts/mia_trace.py view output/mia-trace/success.jsonl
    python scripts/mia_trace.py replay output/mia-trace/success.json

``demo`` executes the real keyless chain twice (success and refusal), verifies replay,
writes allowlist-exported JSON/JSONL plus a Markdown view, and prints the digests a
reviewer needs to bind the artifact to a source revision. It never calls a model, never
opens a network socket and never reads the Owner database.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_agent.run_trace import (  # noqa: E402
    export_trace,
    read_jsonl,
    verify_replay,
    write_jsonl,
)
from market_agent.scripted_research import (  # noqa: E402
    run_filing_research,
    run_refused_forward,
    scenario_filing_research,
)

AS_OF = datetime(2026, 10, 26, tzinfo=timezone.utc)
CAPTURE = ROOT / "evals" / "fixtures" / "mia_trace_eval" / "GLXY.json"


def _load_capture() -> dict:
    return json.loads(CAPTURE.read_text(encoding="utf-8"))


def _render(run_doc: dict) -> str:
    lines = [
        f"# trace `{run_doc['run_id']}`",
        "",
        f"- schema: `{run_doc['schema_version']}`",
        f"- execution_mode: `{run_doc['execution_mode']}`  status: `{run_doc['status']}`"
        + (f"  error: `{run_doc['error_code']}`" if run_doc.get("error_code") else ""),
        f"- code: `{run_doc['code']['commit'][:12]}` ({run_doc['code']['source']})",
        f"- as_of: `{run_doc['as_of']}`  outcome: `{(run_doc.get('outcome_digest') or '')[:23]}…`",
        "",
        "| # | kind | name | status | out | ms | error |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, span in enumerate(run_doc["spans"], start=1):
        lines.append(
            f"| {index} | {span['kind']} | `{span['name']}` | {span['status']} | "
            f"{span.get('output_count') if span.get('output_count') is not None else '-'} | "
            f"{span.get('latency_ms')} | {span.get('error_code') or '-'} |")
    policies = [(span["name"], p) for span in run_doc["spans"] for p in span.get("policies", [])]
    if policies:
        lines += ["", "## Policy decisions", ""]
        for name, policy in policies:
            lines.append(f"- `{name}` {policy['gate']}: **{policy['decision']}**"
                         + (f" ({policy['code']})" if policy.get("code") else ""))
    models = [span["model"] for span in run_doc["spans"] if span.get("model")]
    if models:
        lines += ["", "## Model usage", ""]
        for model in models:
            lines.append(f"- {model['execution']}: provider={model.get('provider')} "
                         f"model={model.get('model')} cost={model['cost']['status']} "
                         f"input={model['input_tokens']['status']}")
    lines.append("")
    return "\n".join(lines)


def demo(out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    capture = _load_capture()

    success = run_filing_research(capture=capture, as_of=AS_OF, symbol="GLXY",
                                  hypothetical_price="24.00")
    success_doc = export_trace(success)
    write_jsonl(success_doc, out / "success.jsonl")
    (out / "success.json").write_text(json.dumps(success_doc, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8", newline="\n")
    (out / "success.md").write_text(_render(success_doc) + "\n", encoding="utf-8", newline="\n")

    refusal = run_refused_forward()
    refusal_doc = export_trace(refusal)
    write_jsonl(refusal_doc, out / "refusal.jsonl")
    (out / "refusal.json").write_text(json.dumps(refusal_doc, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8", newline="\n")
    (out / "refusal.md").write_text(_render(refusal_doc) + "\n", encoding="utf-8", newline="\n")

    replay = verify_replay(success_doc, lambda: scenario_filing_research(
        capture, as_of=AS_OF, symbol="GLXY", hypothetical_price="24.00"))
    (out / "replay.json").write_text(json.dumps(replay.model_dump(mode="json"), indent=2) + "\n",
                                     encoding="utf-8", newline="\n")

    print(f"success  {success.status:8} {success.outcome_digest}")
    print(f"refusal  {refusal.status:8} {refusal.outcome_digest} ({refusal.error_code})")
    print(f"replay   {replay.status}")
    print(f"artifacts -> {out}")
    return 0 if replay.status == "REPLAY_OK" else 1


def view(path: Path) -> int:
    rows = read_jsonl(path) if path.suffix == ".jsonl" else [
        json.loads(path.read_text(encoding="utf-8"))]
    for row in rows:
        print(_render(row))
    return 0


def replay(path: Path) -> int:
    document = json.loads(path.read_text(encoding="utf-8"))
    capture = _load_capture()
    as_of = datetime.fromisoformat(document["as_of"])
    outcome = verify_replay(document, lambda: scenario_filing_research(
        capture, as_of=as_of, symbol="GLXY", hypothetical_price="24.00"))
    print(json.dumps(outcome.model_dump(mode="json"), indent=2))
    return 0 if outcome.status == "REPLAY_OK" else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MIA trace demo/view/replay")
    sub = parser.add_subparsers(dest="command", required=True)
    demo_parser = sub.add_parser("demo")
    demo_parser.add_argument("--out", type=Path, default=Path("output/mia-trace"))
    view_parser = sub.add_parser("view")
    view_parser.add_argument("path", type=Path)
    replay_parser = sub.add_parser("replay")
    replay_parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    if args.command == "demo":
        return demo(args.out)
    if args.command == "view":
        return view(args.path)
    return replay(args.path)


if __name__ == "__main__":
    raise SystemExit(main())
