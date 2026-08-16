"""Heuristic design-review audits and manufacturing helpers.

These are best-effort checks over the schematic/PCB data we can extract. They
flag likely issues (missing decoupling, floating pins, DFM spacing) rather than
performing a full electrical simulation. Each finding includes a severity so a
caller can triage.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


def _is_cap(comp: Dict[str, Any]) -> bool:
    lib = (comp.get("lib_id") or "").lower()
    return lib.endswith(":c") or "capacitor" in lib or ":c_" in lib


def _is_resistor(comp: Dict[str, Any]) -> bool:
    lib = (comp.get("lib_id") or "").lower()
    return lib.endswith(":r") or "resistor" in lib or ":r_" in lib


def _is_ic(sch, comp: Dict[str, Any]) -> bool:
    lib = (comp.get("lib_id") or "").lower()
    if any(k in lib for k in ("mcu", "ic", "regulator", "amplifier", "logic", "stm", "atmega")):
        return True
    try:
        pins = sch.pin_locations(comp["reference"])
    except Exception:
        pins = None
    return bool(pins and len(pins) >= 6)


def _dist(a: Dict[str, float], b: Dict[str, float]) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def audit_decoupling(sch, radius: float = 10.0) -> List[Dict[str, Any]]:
    comps = sch.list_components()
    caps = [c for c in comps if _is_cap(c) and c.get("at")]
    findings = []
    for c in comps:
        if not _is_ic(sch, c) or not c.get("at"):
            continue
        near = [cap for cap in caps if _dist(c["at"], cap["at"]) <= radius]
        if not near:
            findings.append({"severity": "warning", "reference": c["reference"],
                             "issue": "no decoupling cap within %.1fmm" % radius})
    return findings


def audit_connections(sch) -> List[Dict[str, Any]]:
    findings = []
    nets = [n.lower() for n in sch.list_nets()]
    has_i2c = any("sda" in n or "scl" in n for n in nets)
    resistors = [c for c in sch.list_components() if _is_resistor(c)]
    if has_i2c and len(resistors) < 2:
        findings.append({"severity": "warning",
                         "issue": "I2C nets present but few pull-up resistors found"})
    # Floating pins: pins with no wire/label coincident (needs geometry).
    try:
        from .connectivity import SchematicGraph
        g = SchematicGraph(sch)
        for comp in g.components():
            if len(comp["pins"]) == 1 and not comp["nets"]:
                p = comp["pins"][0]
                findings.append({"severity": "info", "reference": p["ref"],
                                 "pin": p["pin"], "issue": "pin on an unnamed single-node net"})
    except Exception:
        pass
    return findings


def audit_power_rails(sch) -> List[Dict[str, Any]]:
    findings = []
    nets = sch.list_nets()
    power_nets = [n for n in nets if n.upper() in ("VCC", "VDD", "+5V", "+3V3", "+3.3V", "VBUS")
                  or n.startswith("+")]
    caps = [c for c in sch.list_components() if _is_cap(c)]
    if power_nets and not caps:
        findings.append({"severity": "warning",
                         "issue": "power rails present but no bulk/decoupling capacitors found"})
    return findings


def audit_manufacturing(board, min_spacing: float = 0.5) -> List[Dict[str, Any]]:
    findings = []
    fps = [f for f in board.list_footprints() if f.get("at")]
    if board.get_board_extents() is None:
        findings.append({"severity": "error", "issue": "no board outline (Edge.Cuts) found"})
    for i in range(len(fps)):
        for j in range(i + 1, len(fps)):
            d = _dist(fps[i]["at"], fps[j]["at"])
            if d < min_spacing:
                findings.append({"severity": "warning",
                                 "issue": "components very close (%.2fmm)" % d,
                                 "refs": [fps[i]["reference"], fps[j]["reference"]]})
    return findings


def check_bom_health(sch) -> Dict[str, Any]:
    comps = sch.list_components()
    no_value = [c["reference"] for c in comps if not c.get("value")]
    no_footprint = [c["reference"] for c in comps
                    if not (c.get("footprint"))]
    return {"total": len(comps), "missing_value": no_value,
            "missing_footprint": no_footprint,
            "healthy": not no_value and not no_footprint}
