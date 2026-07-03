"""Design generator: DesignSpec -> KiCad project on disk.

All electrical values are solved from the SAME strategy assets the repair
loop uses (Vref table, E-series snapping, MPN patterns, LED Vf) — generation
and repair share one evolvable knowledge base, so an AHE promotion improves
both paths at once.
"""

from __future__ import annotations

import json
from pathlib import Path

from ratsnest.agents.repair_planner import format_ohms
from ratsnest.config import Config
from ratsnest.design_gen.templates import build_regulator_board, rail_name
from ratsnest.khlib import load_kh_module
from ratsnest.schemas import DesignSpec, StrategyBundle

REGULATOR_PART = "AP1117-ADJ"  # gen v1 board family: one adjustable LDO
LED_I_TARGET_A = 0.010

DEFAULT_LED_VF = {"red": 2.0, "green": 2.2, "blue": 3.1, "yellow": 2.1,
                  "white": 3.2, "orange": 2.0}
DEFAULT_LED_MPN = {"red": "LTST-C170KRKT", "green": "LTST-C170GKT",
                   "blue": "LTST-C170TBKT", "yellow": "LTST-C170YKT",
                   "white": "LTST-C170AWT", "orange": "LTST-C170KFKT"}


class GenerationError(ValueError):
    pass


def _snap(config: Config, ideal: float, series: str = "E24") -> float:
    utils = load_kh_module("kicad_utils", config.kicad_scripts)
    snapped, _ = utils.snap_to_e_series(ideal, series)
    return float(snapped)


def _vref_for(strategy: StrategyBundle, part: str) -> float:
    for key, vref in strategy.solver_params.get("vref_table", {}).items():
        if key.lower() in part.lower():
            return float(vref)
    raise GenerationError(f"no Vref entry for {part} in strategy vref_table")


def pick_divider(config: Config, target: float, vref: float,
                 tolerance_pct: float = 2.0) -> tuple[float, float, float]:
    """Choose (r_top, r_bottom, achieved_vout): best E24 pair over several
    r_bottom candidates. Raises if no pair lands within tolerance."""
    best = None
    for r_bot in (1000.0, 1200.0, 1500.0, 2000.0):
        ideal_top = r_bot * (target / vref - 1.0)
        if ideal_top <= 0:
            continue
        r_top = _snap(config, ideal_top)
        achieved = vref * (1 + r_top / r_bot)
        dev = abs(achieved - target) / target
        if best is None or dev < best[3]:
            best = (r_top, r_bot, achieved, dev)
    if best is None or best[3] > tolerance_pct / 100.0:
        raise GenerationError(
            f"no E24 divider reaches {target}V within {tolerance_pct}% "
            f"(best: {best[2] if best else None}V)")
    return best[0], best[1], best[2]


def _resistor_mpn(strategy: StrategyBundle, value_str: str) -> str:
    mpn_map = strategy.solver_params.get("mpn_map", {})
    if value_str in mpn_map:
        return str(mpn_map[value_str])
    pattern = strategy.solver_params.get(
        "resistor_mpn_pattern", "RC0805FR-07{code}L")
    # Yageo value code: 3k->3K, 1.6k->1K6, 330->330R, 8.2k->8K2
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


def generate_project(spec: DesignSpec, out_dir: Path,
                     strategy: StrategyBundle,
                     config: Config | None = None) -> Path:
    """Write <out_dir>/<project>.kicad_sch + .kicad_pro (+ designspec.json)."""
    config = config or Config.load()
    if spec.output_voltage >= spec.input_voltage:
        raise GenerationError(
            f"linear regulator needs Vin > Vout "
            f"(got {spec.input_voltage}V -> {spec.output_voltage}V)")

    vref = _vref_for(strategy, REGULATOR_PART)
    tol = float(strategy.solver_params.get("vout_tolerance_pct", 2.0))
    r_top, r_bot, achieved = pick_divider(config, spec.output_voltage, vref, tol)
    r1_str, r2_str = format_ohms(r_top), format_ohms(r_bot)

    values = {"U1": REGULATOR_PART, "R1": r1_str, "R2": r2_str}
    mpn_map = strategy.solver_params.get("mpn_map", {})
    mpns = {"U1": str(mpn_map.get(REGULATOR_PART, "")),
            "R1": _resistor_mpn(strategy, r1_str),
            "R2": _resistor_mpn(strategy, r2_str)}

    include_led = spec.led is not None
    if include_led:
        color = spec.led.lower()
        vf_table = {**DEFAULT_LED_VF,
                    **strategy.solver_params.get("led_vf_by_color", {})}
        vf = float(vf_table.get(color, 2.0))
        headroom = spec.output_voltage - vf
        if headroom <= 0.3:
            raise GenerationError(
                f"{color} LED (Vf={vf}V) has no headroom on a "
                f"{spec.output_voltage}V rail")
        ideal = headroom / LED_I_TARGET_A
        r3 = _snap(config, ideal)
        if r3 < ideal:  # never exceed target current
            r3 = _snap(config, ideal * 1.1)
        r3_str = format_ohms(r3)
        led_mpns = {**DEFAULT_LED_MPN,
                    **strategy.solver_params.get("led_mpn_map", {})}
        values.update({"R3": r3_str, "D1": f"LED_{color.upper()}"})
        mpns.update({"R3": _resistor_mpn(strategy, r3_str),
                     "D1": str(led_mpns.get(color, ""))})

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sch = build_regulator_board(
        project=spec.project_name,
        vin_rail=rail_name(spec.input_voltage),
        vout_rail=rail_name(spec.output_voltage),
        values=values, mpns=mpns, include_led=include_led,
        title=spec.requirement_text[:80] or spec.project_name,
    )
    (out_dir / f"{spec.project_name}.kicad_sch").write_text(sch, encoding="utf-8")
    (out_dir / f"{spec.project_name}.kicad_pro").write_text(
        json.dumps({"meta": {"filename": f"{spec.project_name}.kicad_pro",
                             "version": 1}}, indent=2), encoding="utf-8")
    (out_dir / "designspec.json").write_text(
        spec.model_dump_json(indent=2), encoding="utf-8")
    return out_dir
