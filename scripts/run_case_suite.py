"""Run the case suite and score it (B1 of docs/Production_Readiness_Plan.md).

A single prompt cannot tell a real improvement from run-to-run variance: the same
case has produced 26 nets and 0 nets on different runs. This runs every case in a
directory, extracts the deterministic outcome of each, and writes a comparable
report so a design change can be judged against a baseline.

    .\\scripts\\run_with_ericai.ps1 python scripts/run_case_suite.py
    .\\scripts\\run_with_ericai.ps1 python scripts/run_case_suite.py --cases cases --repeat 2
    python scripts/run_case_suite.py --compare data/ratsnestpro/suite/<a>.json <b>.json

Scoring is read only from tool results and filesystem checks, never from model
prose, so the report cannot be talked up by a narrative.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage

from agents.ratsnestpro.ratsnestpro_agent import (
    _arbitrate_requirement,
    _with_acks,
    ratsnestpro_multi_agent,
)

SUITE_DIR = Path("data/ratsnestpro/suite")


def _score(values: dict[str, Any]) -> dict[str, Any]:
    """Reduce one run's final state to comparable, deterministic numbers."""
    hardware = values.get("hardware") or {}
    verification = hardware.get("verification") or {}
    erc = verification.get("erc") or {}
    drc = verification.get("drc") or {}
    routing = hardware.get("routing") or {}
    evaluations = [e for e in (values.get("change_evaluations") or []) if isinstance(e, dict)]
    steps = [s for s in (hardware.get("steps") or []) if isinstance(s, dict)]
    blocked = [s.get("name") for s in steps if s.get("blocked")]
    return {
        "workflow_mode": values.get("workflow_mode"),
        "release_ready": bool(hardware.get("release_ready")),
        "completed_steps": hardware.get("completed_steps") or 0,
        "first_blocked_step": blocked[0] if blocked else None,
        "erc_errors": erc.get("errors"),
        "drc_errors": drc.get("errors"),
        "routing_method": routing.get("method"),
        "unconnected": routing.get("unconnected"),
        "repair_rounds": len(evaluations),
        "verdicts": [e.get("verdict") for e in evaluations],
        "release_blockers": hardware.get("release_blockers") or [],
        "artifacts": len(hardware.get("actual_files") or []),
        "review_status": (values.get("review") or {}).get("status"),
    }


async def _stream(text: str, config: dict[str, Any]) -> dict[str, Any]:
    """One turn, reduced to the accumulated state updates it produced."""
    values: dict[str, Any] = {}
    async for chunk in ratsnestpro_multi_agent.astream(
        {"messages": [HumanMessage(content=text)]},
        config=config,
        stream_mode="updates",
    ):
        if isinstance(chunk, dict):
            for update in chunk.values():
                if isinstance(update, dict):
                    values.update(update)
    return values


async def _run_case(path: Path) -> dict[str, Any]:
    requirement = path.read_text(encoding="utf-8")
    thread_id = str(uuid4())
    # Distinct run_name prefixes keep two engineers from contending on the same
    # run-directory lock when they benchmark at the same time.
    config = {
        "configurable": {
            "thread_id": thread_id,
            "client_thread_id": thread_id,
            "run_name": f"suite-{path.stem}-{thread_id[:8]}",
        },
        "recursion_limit": 50,
    }
    started = datetime.now(UTC)
    # Risk arbitration can stop a run before step 1 and wait for the user to
    # accept a datasheet conflict found in the requirement itself. Scoring that
    # as 0/17 would say nothing about the 17 steps this suite exists to measure.
    #
    # The acceptance is computed here rather than answered in a second turn.
    # Arbitration is Tier 1 only — deterministic, no model call — so asking it
    # directly costs nothing, and a second turn does not work at this entry
    # point: without a checkpointer the reply arrives as a NEW request, the
    # original requirement is gone, and the run scores 0/17 for a different
    # reason than the one being fixed.
    #
    # Recorded, because "this case needs N risks accepted before it can build" is
    # itself a result, and a change in that number belongs in a comparison.
    acked = sorted(
        token
        for token in (v.ack_token for v in _arbitrate_requirement(requirement).blocking)
        if token
    )
    values = await _stream(_with_acks(requirement, set(acked)), config)
    elapsed = (datetime.now(UTC) - started).total_seconds()
    result = {
        "case": path.stem,
        "elapsed_s": round(elapsed, 1),
        "acked_risks": acked,
        **_score(values),
    }
    print(
        f"  {path.stem:38} {result['completed_steps']:>2}/17  "
        f"erc={result['erc_errors']}  blocked_at={result['first_blocked_step']}  "
        f"rounds={result['repair_rounds']}  acks={len(acked)}  {elapsed:.0f}s",
        flush=True,
    )
    return result


def _report(cases: list[Path], repeat: int, runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "cases": [c.stem for c in cases],
        "repeat": repeat,
        "runs": runs,
        "summary": summarize(runs),
    }


async def run_suite(cases_dir: Path, repeat: int, out: Path) -> dict[str, Any]:
    cases = sorted(p for p in cases_dir.glob("*.md") if p.stem != "README")
    if not cases:
        raise SystemExit(f"no cases found in {cases_dir}")
    print(f"suite: {len(cases)} case(s) x {repeat} repeat(s)\n", flush=True)
    runs: list[dict[str, Any]] = []
    for attempt in range(1, repeat + 1):
        print(f"pass {attempt}/{repeat}", flush=True)
        for case in cases:
            runs.append({"pass": attempt, **await _run_case(case)})
            # Flushed after every case rather than once at the end. One case can
            # take half an hour, so a report that only exists on clean exit means
            # an interrupted benchmark leaves nothing behind — and the runs that
            # did finish are still comparable.
            out.write_text(
                json.dumps(_report(cases, repeat, runs), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    return _report(cases, repeat, runs)


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate, and expose spread so variance is visible rather than hidden."""
    total = len(runs)
    completed = [r["completed_steps"] for r in runs]
    erc = [r["erc_errors"] for r in runs if isinstance(r.get("erc_errors"), int)]
    verdicts: dict[str, int] = {}
    for run in runs:
        for verdict in run.get("verdicts") or []:
            verdicts[str(verdict)] = verdicts.get(str(verdict), 0) + 1
    return {
        "runs": total,
        "release_ready_rate": round(sum(r["release_ready"] for r in runs) / total, 3),
        "completed_steps_mean": round(statistics.fmean(completed), 2) if completed else 0,
        "completed_steps_min": min(completed) if completed else 0,
        "completed_steps_max": max(completed) if completed else 0,
        "erc_errors_mean": round(statistics.fmean(erc), 2) if erc else None,
        "erc_errors_min": min(erc) if erc else None,
        "erc_errors_max": max(erc) if erc else None,
        "repair_rounds_mean": round(statistics.fmean([r["repair_rounds"] for r in runs]), 2),
        "verdict_distribution": dict(sorted(verdicts.items())),
        "blocked_at": dict(
            sorted(
                {
                    str(r["first_blocked_step"]): sum(
                        1 for x in runs if x["first_blocked_step"] == r["first_blocked_step"]
                    )
                    for r in runs
                }.items()
            )
        ),
    }


def compare(before: Path, after: Path) -> None:
    """Print the deltas that matter, so a change is judged against a baseline."""
    a = json.loads(before.read_text(encoding="utf-8"))["summary"]
    b = json.loads(after.read_text(encoding="utf-8"))["summary"]
    print(f"{'metric':28} {'before':>12} {'after':>12}")
    for key in (
        "release_ready_rate",
        "completed_steps_mean",
        "completed_steps_min",
        "erc_errors_mean",
        "repair_rounds_mean",
    ):
        print(f"{key:28} {str(a.get(key)):>12} {str(b.get(key)):>12}")
    print(f"\nverdicts before: {a.get('verdict_distribution')}")
    print(f"verdicts after : {b.get('verdict_distribution')}")
    print(f"blocked before : {a.get('blocked_at')}")
    print(f"blocked after  : {b.get('blocked_at')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="cases", help="directory of .md case files")
    parser.add_argument("--repeat", type=int, default=1, help="passes over the suite")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    args = parser.parse_args()

    if args.compare:
        compare(Path(args.compare[0]), Path(args.compare[1]))
        return 0

    SUITE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = SUITE_DIR / f"suite-{stamp}.json"
    report = asyncio.run(run_suite(Path(args.cases), max(1, args.repeat), out))
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsummary: {json.dumps(report['summary'], ensure_ascii=False)}")
    print(f"report : {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
