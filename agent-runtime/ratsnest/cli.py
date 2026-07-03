"""RatsNest CLI.

    python -m ratsnest evaluate <project_dir> [--json]
    python -m ratsnest fix <project_dir> [--max-iter N] [--suggest-only] [--json]
    python -m ratsnest evolve [--boards N] [--promote]
    python -m ratsnest stats
    python -m ratsnest seed-defects
    python -m ratsnest export-schemas
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ratsnest.config import REPO_ROOT, Config
from ratsnest.evolution import StrategyRegistry
from ratsnest.kh_adapter import KicadHappyAdapter
from ratsnest.schemas import RunConfig


def _print_scorecard(sc, findings) -> None:
    print(f"  score: {sc.score}/100   findings: {sc.findings_total} "
          f"(suppressed {sc.suppressed_total})   severity: {sc.severity_counts}")
    for f in findings:
        if f.severity in ("error", "warning"):
            print(f"    [{f.severity:7s}] {f.rule_id or f.detector}: "
                  f"{(f.model_extra or {}).get('summary', '')[:90]}")


def cmd_evaluate(args) -> int:
    from ratsnest.agents import synthesize
    config = Config.load()
    _, strategy = StrategyRegistry(config.strategies_dir).load_active()
    outputs = KicadHappyAdapter(config).analyze_all(Path(args.project_dir))
    ev = synthesize(outputs, strategy, args.project_dir)
    if args.json:
        print(ev.model_dump_json(indent=2, exclude={"analyzer_outputs"}))
    else:
        print(f"evaluate {args.project_dir}  [strategy {strategy.version_id()}]")
        _print_scorecard(ev.scorecard, ev.findings)
    return 0


def cmd_fix(args) -> int:
    from ratsnest.orchestrator import RunLoop
    rc = RunConfig(project_dir=str(Path(args.project_dir).resolve()),
                   max_iterations=args.max_iter,
                   fix_policy="suggest_only" if args.suggest_only else "auto",
                   run_erc=not args.no_erc)
    record = RunLoop().execute(rc)
    if args.json:
        print(record.model_dump_json(indent=2))
        return 0
    print(f"run {record.run_id}  status={record.status}  "
          f"strategy={record.strategy_version_id}")
    for it in record.iterations:
        ops = len(it.patch_plan.ops) if it.patch_plan else 0
        print(f"  iter {it.iteration}: score={it.scorecard.score}  "
              f"delta={it.score_delta:+.1f}  ops={ops}  "
              f"resolved={len(it.resolved_findings)}")
        if it.patch_plan:
            for fid, why in it.patch_plan.rationale.items():
                print(f"      {fid}: {why}")
    if record.escalation:
        print(f"  escalation: {record.escalation}")
    return 0 if record.status in ("converged", "suggested") else 1


def cmd_evolve(args) -> int:
    from ratsnest.evolution.experiment import run_default_experiment
    report = run_default_experiment(promote=args.promote,
                                    candidate=args.candidate)
    print(f"experiment {report.experiment_id}: candidate "
          f"'{report.candidate_name}' vs incumbent")
    print(f"  mean score: incumbent={report.mean_incumbent_score:.1f}  "
          f"candidate={report.mean_candidate_score:.1f}")
    for row in report.per_board:
        print(f"  {row['board']}: {row['incumbent_score']:.1f} -> "
              f"{row['candidate_score']:.1f}  new_errors={row['new_errors']}")
    print("  gates:")
    for gate, ok in report.gates.items():
        print(f"    [{'PASS' if ok else 'FAIL'}] {gate}: "
              f"{report.gate_reasons.get(gate, '')}")
    print(f"  promoted: {report.promoted}")
    return 0


def cmd_stats(args) -> int:
    from ratsnest.evolution.triggers import compute_stats, propose_surface
    stats = compute_stats(Config.load().runs_dir)
    print(json.dumps(stats, indent=2))
    print("proposal:", propose_surface(stats))
    return 0


def cmd_seed(args) -> int:
    sys.path.insert(0, str(REPO_ROOT / "benchmarks"))
    import seed_defects
    seed_defects.seed()
    return 0


def cmd_export_schemas(args) -> int:
    from ratsnest.schemas.export import export_all
    for p in export_all():
        print(p)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ratsnest")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("evaluate", help="analyze + synthesize -> scorecard")
    p.add_argument("project_dir")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("fix", help="run the closed repair loop")
    p.add_argument("project_dir")
    p.add_argument("--max-iter", type=int, default=4)
    p.add_argument("--suggest-only", action="store_true")
    p.add_argument("--no-erc", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_fix)

    p = sub.add_parser("evolve", help="run an AHE experiment (offline)")
    p.add_argument("--promote", action="store_true",
                   help="promote the candidate if all gates pass")
    p.add_argument("--candidate", default=None,
                   help="name of a strategies/<name> dir to evaluate; "
                        "default: auto-generated candidate")
    p.set_defaults(func=cmd_evolve)

    p = sub.add_parser("stats", help="trigger statistics from ATDP trajectories")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("seed-defects", help="regenerate the defective benchmark board")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("export-schemas", help="export JSON Schemas for the control plane")
    p.set_defaults(func=cmd_export_schemas)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
