"""Run the frozen adversarial corpus and write a machine-readable eval report.

    python evals/run_trace_eval.py
    python evals/run_trace_eval.py --json output/trace-eval.json --markdown output/trace-eval.md

Exit code is 0 only when every case passes, so CI can gate on it. The report always
records the exact code SHA, dataset version and command, because a pass rate without
those three is not reproducible.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from evals.trace_eval import evaluate, render_markdown  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MIA trace/eval corpus runner")
    parser.add_argument("--as-of", default=None, help="ISO instant used as the scenario as_of")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    command = "python evals/run_trace_eval.py" + (
        f" --as-of {args.as_of}" if args.as_of else "")
    report = evaluate(as_of=as_of, command=command)
    markdown = render_markdown(report)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8", newline="\n")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown + "\n", encoding="utf-8", newline="\n")
    if not args.quiet:
        print(markdown)
    else:
        totals = report["totals"]
        print(f"{totals['pass_rate']} passed  digest {report['dataset_digest'][:23]}  "
              f"code {report['code']['commit'][:12]}")
        for failure in report["named_failures"]:
            print(f"  FAIL {failure['case_id']}: {'; '.join(failure['failures'])}")
    return 0 if report["totals"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
