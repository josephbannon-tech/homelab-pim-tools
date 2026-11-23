"""Run the eval set against one brain and write a report.

  python -m evals.run oracle
  python -m evals.run shim --shim ~/.local/bin/claude-chat-shim
  python -m evals.run openai --base-url http://host:8080/v1 --model some-model

Exit status is the gate: 0 when at least 90% of cases pass and no write
reached the store that a case did not ask for, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .brains import Brain, OracleBrain, ShimBrain, ToolLoopBrain
from .score import PASS_RATE, Result, gate, load_cases, score_case
from .server import EvalServer

RESULTS = Path(__file__).with_name("results")


def run(brain: Brain, server: EvalServer, cases: list[dict], log=print) -> list[Result]:
    results = []
    for case in cases:
        store = server.reset(case["seed"])
        messages = [*case.get("history", []), {"role": "user", "content": case["prompt"]}]
        t0, reply, error = time.monotonic(), "", None
        try:
            reply = brain.ask(messages, case)
        except Exception as e:  # a brain that falls over fails the case, not the run
            error = f"{type(e).__name__}: {e}"
        result = score_case(case, store, reply, error)
        result.seconds = time.monotonic() - t0
        results.append(result)
        mark = "pass" if result.passed else "FAIL"
        log(f"{mark}  {result.seconds:6.1f}s  {case['id']}  {'; '.join(result.reasons)}")
    return results


def report(brain_name: str, results: list[Result], when: datetime) -> str:
    ok, rate, bad = gate(results)
    passed = sum(r.passed for r in results)
    times = [r.seconds for r in results]
    lines = [
        f"# Eval run: {brain_name}",
        "",
        f"- Date: {when:%Y-%m-%d %H:%M} ({when.tzinfo})",
        f"- Cases passed: {passed}/{len(results)} ({rate:.0%}, gate {PASS_RATE:.0%})",
        f"- Bad writes: {bad} (gate 0)",
        f"- Gate: {'PASS' if ok else 'FAIL'}",
        f"- Seconds per turn: median {statistics.median(times):.1f}, max {max(times):.1f}",
        "",
        "| Category | Passed |",
        "|---|---|",
    ]
    for cat in dict.fromkeys(r.category for r in results):
        rows = [r for r in results if r.category == cat]
        lines.append(f"| {cat} | {sum(r.passed for r in rows)}/{len(rows)} |")
    failures = [r for r in results if not r.passed]
    if failures:
        lines += ["", "## Failures", ""]
        for r in failures:
            lines.append(f"### {r.id}")
            lines += [f"- {reason}" for reason in r.reasons]
            calls = ", ".join(f"{c['tool']}{'' if c['ok'] else ' (failed)'}" for c in r.calls)
            lines.append(f"- calls: {calls or 'none'}")
            lines.append(f"- reply: {' '.join(r.reply.split())[:300]}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__.split("\n")[0])
    p.add_argument("brain", choices=["oracle", "shim", "openai"])
    p.add_argument("--shim", help="path to the shim script (brain: shim)")
    p.add_argument("--base-url", help="OpenAI-compatible base URL ending in /v1 (brain: openai)")
    p.add_argument("--model", help="model id (brain: openai)")
    p.add_argument("--system-extra", default="", help="text appended to the system prompt (openai)")
    p.add_argument("--label", help="name the report and results files after this, not the brain")
    p.add_argument("--timezone", default="Europe/London")
    p.add_argument("--only", nargs="*", help="run just these case ids")
    p.add_argument("--no-write", action="store_true", help="do not write the results files")
    args = p.parse_args(argv)

    tz = ZoneInfo(args.timezone)
    when = datetime.now(tz)
    cases = load_cases(when.date(), tz)
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]

    with EvalServer() as server:
        if args.brain == "oracle":
            brain: Brain = OracleBrain(server.url)
        elif args.brain == "shim":
            if not args.shim:
                p.error("--shim is required for the shim brain")
            brain = ShimBrain(str(Path(args.shim).expanduser()), server.url, tz)
        else:
            if not (args.base_url and args.model):
                p.error("--base-url and --model are required for the openai brain")
            brain = ToolLoopBrain(args.base_url, args.model, server.url, tz, args.system_extra)
        try:
            results = run(brain, server, cases)
        finally:
            brain.close()

    label = args.label or brain.name
    text = report(label, results, when)
    print("\n" + text)
    if not args.no_write and not args.only:
        RESULTS.mkdir(exist_ok=True)
        stem = f"{when:%Y-%m-%d}-{label}"
        (RESULTS / f"{stem}.md").write_text(text)
        (RESULTS / f"{stem}.json").write_text(
            json.dumps([r.__dict__ for r in results], indent=2, default=str) + "\n"
        )
    return 0 if gate(results)[0] else 1


if __name__ == "__main__":
    sys.exit(main())
