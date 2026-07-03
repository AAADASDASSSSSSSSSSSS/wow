"""Repair planner: findings × strategy mapping table -> RepairHints -> PatchPlan.

Discipline (design doc §4.5 risks): repairs come ONLY from the rule_id→mapping
table plus deterministic solvers. Unmapped findings are escalated, never
improvised. E-series snapping reuses kicad-happy's kicad_utils (unforked).
"""

from __future__ import annotations

import re

from ratsnest.config import Config
from ratsnest.khlib import load_kh_module
from ratsnest.schemas import (
    EvaluationResult,
    Finding,
    PatchPlan,
    RepairHint,
    RepairMapping,
    RepairOp,
    RepairOpType,
    StrategyBundle,
)


def format_ohms(value: float) -> str:
    """3000 -> '3k', 4700 -> '4.7k', 330 -> '330', 1_500_000 -> '1.5M'."""
    for factor, suffix in ((1e6, "M"), (1e3, "k")):
        if value >= factor:
            v = value / factor
            s = f"{v:.2f}".rstrip("0").rstrip(".")
            return f"{s}{suffix}"
    s = f"{value:.2f}".rstrip("0").rstrip(".")
    return s


def _snap(config: Config, ideal: float, series: str) -> float:
    utils = load_kh_module("kicad_utils", config.kicad_scripts)
    snapped, _err = utils.snap_to_e_series(ideal, series)
    return float(snapped)


def resistor_mpn(strategy: StrategyBundle, value_str: str) -> str:
    """Curated MPN for a resistor value: explicit map first, else the
    strategy's Yageo-style pattern (3k->3K, 1.6k->1K6, 330->330R)."""
    mpn_map: dict = strategy.solver_params.get("mpn_map", {})
    if value_str in mpn_map:
        return str(mpn_map[value_str])
    pattern = strategy.solver_params.get(
        "resistor_mpn_pattern", "RC0805FR-07{code}L")
    if value_str.endswith(("k", "M")):
        suffix = value_str[-1].upper()
        body = value_str[:-1]
        if "." in body:
            whole, frac = body.split(".")
            code = f"{whole}{suffix}{frac}"
        else:
            code = f"{body}{suffix}"
    else:
        code = f"{value_str}R"
    return pattern.format(code=code)


# ---------------------------------------------------------------------------
# Solvers — each returns list[RepairOp] (possibly empty) + explanation
# ---------------------------------------------------------------------------

def _solve_feedback_divider(f: Finding, mapping: RepairMapping,
                            strategy: StrategyBundle, config: Config,
                            ctx: dict) -> tuple[list[RepairOp], str]:
    extra = f.model_extra or {}
    r_top, r_bot = extra.get("r_top") or {}, extra.get("r_bottom") or {}
    vref, target = extra.get("vref"), extra.get("target_vout")
    if not (r_top.get("ohms") and r_bot.get("ohms") and vref and target):
        return [], "missing divider payload"
    series = str(mapping.params.get("e_series", "E24"))
    # keep r_bottom, solve r_top: Vout = Vref * (1 + Rt/Rb)
    ideal = r_bot["ohms"] * (target / vref - 1.0)
    snapped = _snap(config, ideal, series)
    new_value = format_ohms(snapped)
    achieved = vref * (1 + snapped / r_bot["ohms"])
    op = RepairOp(op=RepairOpType.set_value, ref=r_top["ref"],
                  params={"value": new_value}, finding_id=f.finding_id())
    ctx.setdefault("planned_values", {})[r_top["ref"]] = new_value
    return [op], (f"set {r_top['ref']}={new_value} ({series} snap of "
                  f"{ideal:.1f}Ω) -> Vout {achieved:.3g}V vs target {target}V")


def _solve_led_resistor(f: Finding, mapping: RepairMapping,
                        strategy: StrategyBundle, config: Config,
                        ctx: dict) -> tuple[list[RepairOp], str]:
    fp = (f.model_extra or {}).get("fix_params") or {}
    if fp.get("type") != "resistor_value_change" or not fp.get("component"):
        return [], "no usable fix_params on finding"
    # formula like: "R = (Vrail - Vf) / Iled = (5.0 - 1.8) / 0.01"
    m = re.search(r"\(\s*([\d.]+)\s*-\s*([\d.]+)\s*\)\s*/\s*([\d.]+)",
                  fp.get("formula", ""))
    if not m:
        return [], f"unparseable fix formula: {fp.get('formula')!r}"
    vrail, vf, i_target = (float(m.group(i)) for i in (1, 2, 3))
    if i_target <= 0:
        return [], "non-positive target current"
    series = str(mapping.params.get("e_series", "E24"))
    ideal = (vrail - vf) / i_target
    snapped = _snap(config, ideal, series)
    if snapped < ideal:  # never snap below: current must not exceed target
        snapped = _snap(config, ideal * 1.1, series)
    ref = fp["component"]
    new_value = format_ohms(snapped)
    op = RepairOp(op=RepairOpType.set_value, ref=ref,
                  params={"value": new_value}, finding_id=f.finding_id())
    ctx.setdefault("planned_values", {})[ref] = new_value
    return [op], (f"set {ref}={new_value} for ~{(vrail - vf) / snapped * 1000:.1f}mA "
                  f"(target {i_target * 1000:.0f}mA)")


def _solve_fill_mpn(f: Finding, mapping: RepairMapping,
                    strategy: StrategyBundle, config: Config,
                    ctx: dict) -> tuple[list[RepairOp], str]:
    mpn_map: dict = strategy.solver_params.get("mpn_map", {})
    components: list[dict] = ctx.get("components", [])
    planned_values: dict = ctx.get("planned_values", {})
    already: set = ctx.setdefault("mpn_filled", set())
    ops, misses = [], []
    for comp in components:
        ref = comp.get("reference", "")
        if not ref or ref in already or comp.get("mpn"):
            continue
        # if this plan also changes the value, look up MPN for the NEW value
        value = planned_values.get(ref, comp.get("value", ""))
        mpn = mpn_map.get(value)
        if not mpn and comp.get("type") == "resistor":
            mpn = resistor_mpn(strategy, value)  # pattern fallback
        if not mpn:
            misses.append(f"{ref}({value})")
            continue
        ops.append(RepairOp(op=RepairOpType.set_property, ref=ref,
                            params={"name": "MPN", "value": str(mpn)},
                            finding_id=f.finding_id()))
        already.add(ref)
    note = f"filled {len(ops)} MPNs from curated map"
    if misses:
        note += f"; no mapping for {', '.join(misses)} (escalate)"
    return ops, note


_SOLVERS = {
    "feedback_divider": _solve_feedback_divider,
    "led_resistor": _solve_led_resistor,
    "fill_mpn": _solve_fill_mpn,
}


def _mapping_for(f: Finding, strategy: StrategyBundle) -> RepairMapping | None:
    for m in strategy.repair_mappings:
        if not m.enabled:
            continue
        if m.match_rule_id and m.match_rule_id == f.rule_id:
            return m
        if m.match_detector and m.match_detector == f.detector:
            return m
    return None


def plan_repairs(
    evaluation: EvaluationResult,
    strategy: StrategyBundle,
    run_id: str = "",
    iteration: int = 0,
    config: Config | None = None,
) -> tuple[PatchPlan, list[RepairHint], list[Finding]]:
    """Returns (plan, hints, escalations). Escalations = actionable findings
    (error/warning) with no mapping or no solvable ops."""
    config = config or Config.load()
    sch = evaluation.analyzer_outputs.get("schematic")
    ctx: dict = {
        "components": ((sch.model_extra or {}).get("components", []) if sch else []),
    }

    # value-changing solvers must run before fill_mpn (MPN follows new value)
    actionable = [f for f in evaluation.findings if f.severity in ("error", "warning")]
    order = {"feedback_divider": 0, "led_resistor": 0, "fill_mpn": 1}
    matched: list[tuple[Finding, RepairMapping]] = []
    escalations: list[Finding] = []
    for f in actionable:
        m = _mapping_for(f, strategy)
        (matched.append((f, m)) if m else escalations.append(f))
    matched.sort(key=lambda fm: order.get(fm[1].repair_type, 2))

    hints: list[RepairHint] = []
    all_ops: list[RepairOp] = []
    rationale: dict[str, str] = {}
    for f, m in matched:
        solver = _SOLVERS.get(m.repair_type)
        if solver is None:
            escalations.append(f)
            continue
        ops, explanation = solver(f, m, strategy, config, ctx)
        if not ops:
            escalations.append(f)
            continue
        hints.append(RepairHint(
            finding_id=f.finding_id(), rule_id=f.rule_id, severity=f.severity,
            repair_type=m.repair_type, targets=[op.ref for op in ops],
            suggested_ops=ops, confidence=f.confidence or "heuristic",
            explanation=explanation,
        ))
        all_ops.extend(ops)
        rationale[f.finding_id()] = explanation

    plan = PatchPlan(run_id=run_id, iteration=iteration, ops=all_ops,
                     strategy_version_id=strategy.version_id(),
                     rationale=rationale)
    return plan, hints, escalations
