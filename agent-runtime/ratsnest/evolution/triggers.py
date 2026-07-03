"""Trigger statistics from captured ATDP trajectories (paper [1] §5).

Statistics — not anecdotes — decide when to evolve and which surface to
mutate: escalation clusters point at missing repair mappings / solver params,
veto clusters point at bad solver output, healthy runs propose no-op.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def compute_stats(runs_dir: Path) -> dict:
    runs_dir = Path(runs_dir)
    stats = {
        "runs": 0, "converged": 0, "escalated": 0, "vetoes": 0,
        "mean_final_reward": 0.0,
        "escalated_rule_ids": {}, "planned_repair_types": {},
    }
    rewards: list[float] = []
    escalated: Counter = Counter()
    planned: Counter = Counter()

    for traj in sorted(runs_dir.glob("*/trajectory.jsonl")):
        stats["runs"] += 1
        for line in traj.read_text(encoding="utf-8").splitlines():
            evt = json.loads(line)
            node, outcome = evt.get("node"), evt.get("outcome", {})
            if node == "plan_repairs":
                for rid in evt.get("action", {}).get("escalated_rule_ids", []):
                    if rid:
                        escalated[rid] += 1
                for hint in evt.get("agent_state", {}).get("hints", []):
                    planned[hint.get("repair_type", "?")] += 1
            elif node == "verify" and outcome.get("vetoed"):
                stats["vetoes"] += 1
            elif node == "finish":
                if outcome.get("status") == "converged":
                    stats["converged"] += 1
                elif outcome.get("status") == "escalated":
                    stats["escalated"] += 1
                if evt.get("reward") is not None:
                    rewards.append(float(evt["reward"]))

    if rewards:
        stats["mean_final_reward"] = round(sum(rewards) / len(rewards), 2)
    stats["escalated_rule_ids"] = dict(escalated.most_common())
    stats["planned_repair_types"] = dict(planned.most_common())
    return stats


def propose_surface(stats: dict) -> str:
    """Map trajectory statistics to an intervention-surface proposal."""
    esc = stats.get("escalated_rule_ids", {})
    if esc:
        rule, count = next(iter(esc.items()))
        return (f"skill-patch surface: {count} runs escalated on {rule} — "
                f"propose a repair mapping or solver_params extension for it")
    if stats.get("vetoes", 0) > 0:
        return ("skill-patch surface: patches were vetoed — review solver "
                "params (values computed by solvers made boards worse)")
    if stats.get("runs", 0) and stats.get("escalated", 0) == 0:
        return "no-op: runs converge cleanly, nothing to evolve"
    return "insufficient data: capture more runs before evolving"
