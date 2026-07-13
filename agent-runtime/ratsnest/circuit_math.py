"""Circuit-domain math: values, dividers, MPNs — the ONE evolvable knowledge
base shared by design generation (all backends) AND the repair loop.

AHE invariant: every electrical value is solved from the SAME strategy
assets (Vref table, E-series snapping, MPN patterns, LED Vf), so when the
Evolution Agent promotes a strategy, generation and repair improve together.
E-series snapping reuses kicad-happy's kicad_utils (unforked).
"""

from __future__ import annotations

from ratsnest.config import Config
from ratsnest.khlib import load_kh_module
from ratsnest.schemas import DesignSpec, StrategyBundle

REGULATOR_PART = "AP1117-ADJ"  # board family v1: one adjustable LDO
LED_I_TARGET_A = 0.010

DEFAULT_LED_VF = {"red": 2.0, "green": 2.2, "blue": 3.1, "yellow": 2.1,
                  "white": 3.2, "orange": 2.0}
DEFAULT_LED_MPN = {"red": "LTST-C170KRKT", "green": "LTST-C170GKT",
                   "blue": "LTST-C170TBKT", "yellow": "LTST-C170YKT",
                   "white": "LTST-C170AWT", "orange": "LTST-C170KFKT"}


class GenerationError(ValueError):
    pass


def snap_e_series(config: Config, ideal: float, series: str = "E24") -> float:
    """Snap to the nearest E-series value via kicad-happy's kicad_utils."""
    utils = load_kh_module("kicad_utils", config.kicad_scripts)
    snapped, _ = utils.snap_to_e_series(ideal, series)
    return float(snapped)


def format_ohms(value: float) -> str:
    """3000 -> '3k', 4700 -> '4.7k', 330 -> '330', 1_500_000 -> '1.5M'."""
    for factor, suffix in ((1e6, "M"), (1e3, "k")):
        if value >= factor:
            v = value / factor
            s = f"{v:.2f}".rstrip("0").rstrip(".")
            return f"{s}{suffix}"
    s = f"{value:.2f}".rstrip("0").rstrip(".")
    return s


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
        r_top = snap_e_series(config, ideal_top)
        achieved = vref * (1 + r_top / r_bot)
        dev = abs(achieved - target) / target
        if best is None or dev < best[3]:
            best = (r_top, r_bot, achieved, dev)
    if best is None or best[3] > tolerance_pct / 100.0:
        raise GenerationError(
            f"no E24 divider reaches {target}V within {tolerance_pct}% "
            f"(best: {best[2] if best else None}V)")
    return best[0], best[1], best[2]


def solve_board_values(spec: DesignSpec, strategy: StrategyBundle,
                       config: Config | None = None,
                       regulator_part: str = REGULATOR_PART,
                       ) -> tuple[dict[str, str], dict[str, str], bool]:
    """Solve all component values + MPNs for the regulator board family.

    Shared by BOTH generation backends (template writer and the KiCAD MCP
    executor), so the strategy's Vref table / E-series / MPN assets govern
    every path a design can be created through.

    Returns (values, mpns, include_led).
    """
    config = config or Config.load()
    if spec.output_voltage >= spec.input_voltage:
        raise GenerationError(
            f"linear regulator needs Vin > Vout "
            f"(got {spec.input_voltage}V -> {spec.output_voltage}V)")

    vref = _vref_for(strategy, regulator_part)
    tol = float(strategy.solver_params.get("vout_tolerance_pct", 2.0))
    r_top, r_bot, _achieved = pick_divider(config, spec.output_voltage, vref, tol)
    r1_str, r2_str = format_ohms(r_top), format_ohms(r_bot)

    values = {"U1": regulator_part, "R1": r1_str, "R2": r2_str}
    mpn_map = strategy.solver_params.get("mpn_map", {})
    mpns = {"U1": str(mpn_map.get(regulator_part, "")),
            "R1": resistor_mpn(strategy, r1_str),
            "R2": resistor_mpn(strategy, r2_str)}

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
        r3 = snap_e_series(config, ideal)
        if r3 < ideal:  # never exceed target current
            r3 = snap_e_series(config, ideal * 1.1)
        r3_str = format_ohms(r3)
        led_mpns = {**DEFAULT_LED_MPN,
                    **strategy.solver_params.get("led_mpn_map", {})}
        values.update({"R3": r3_str, "D1": f"LED_{color.upper()}"})
        mpns.update({"R3": resistor_mpn(strategy, r3_str),
                     "D1": str(led_mpns.get(color, ""))})
    return values, mpns, include_led
