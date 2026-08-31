"""Stable failure taxonomy for deterministic pipeline checks.

The decisions/local-evidence snapshot referenced this module but did not ship
it.  Check names are discovered from the colocated pipeline source so newly
added checks cannot silently fall outside the AHE taxonomy.  Classification is
conservative: it selects a repair *scope*, never changes a check verdict.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

_LEGAL_CLASSES = frozenset(
    {
        "constraint_violation",
        "missing_component",
        "missing_support_network",
        "symbol_unavailable",
        "symbol_mismatch",
        "footprint_mismatch",
        "pin_conflict",
        "erc_violation",
        "tool_unavailable",
        "transient_external_failure",
        "routing_congestion",
        "manufacturing_violation",
        "harness_defect",
    }
)


def base_name(name: str) -> str:
    """Remove component/reference suffixes from a pipeline check name."""

    return (name or "").strip().split(":", 1)[0]


def _discover_check_names() -> set[str]:
    source = Path(__file__).with_name("pipeline.py").read_text(encoding="utf-8")
    return {
        base_name(match.group(1))
        for match in re.finditer(r'name\s*=\s*f?"([a-z0-9_.]+)', source)
        if base_name(match.group(1))
    }


_EXPLICIT: dict[str, str] = {
    "datasheet_limits": "constraint_violation",
    "input_voltage_rating": "constraint_violation",
    "requested_layer_count": "constraint_violation",
    "requested_mcu_selected": "symbol_mismatch",
    "component_identity": "symbol_mismatch",
    "symbol": "symbol_unavailable",
    "mcu_footprint": "footprint_mismatch",
    "footprint": "footprint_mismatch",
    "pin_pad_compatibility": "footprint_mismatch",
    "llm_proposal_failed": "transient_external_failure",
    "signals_routed": "routing_congestion",
    "drc_clean": "manufacturing_violation",
    "kicad_cli_erc": "tool_unavailable",
    "datasheet_connection": "erc_violation",
    "mcu_supply_decoupling_not_excessive": "erc_violation",
    "prepared_components_accounted": "missing_component",
    "component_release_ready": "missing_component",
    "external_assets_validated": "transient_external_failure",
    "catalog_evidence_available": "transient_external_failure",
}


def _classify(name: str) -> str:
    if name in _EXPLICIT:
        return _EXPLICIT[name]
    if name.startswith("tool_unavailable."):
        return "tool_unavailable"
    if any(token in name for token in ("footprint", "pad_compatibility")):
        return "footprint_mismatch"
    if any(
        token in name
        for token in (
            "pullup",
            "decoupling",
            "termination",
            "tvs",
            "esd",
            "overvoltage",
            "buck_reference",
            "switching_regulator",
            "power_mux",
            "safe_chain",
            "common_mode",
        )
    ):
        return "missing_support_network"
    if any(
        token in name
        for token in (
            "additional_part",
            "has_parts",
            "selected_components",
            "topology_blocks",
            "external_connector",
        )
    ):
        return "missing_component"
    if any(
        token in name
        for token in (
            "multiple_nets",
            "double_assigned",
            "logical_pins_resolve",
            "mapped_pins_exist",
            "all_pins_resolved",
            "pin_count",
            "requested_pin_used",
        )
    ):
        return "pin_conflict"
    if any(
        token in name
        for token in (
            "clearance",
            "track_width",
            "via",
            "annular",
            "fab_",
            "gerber",
            "bom_written",
            "cpl_written",
            "board_outline",
            "within_board",
            "courtyard",
            "grid_aligned",
            "legal_rotation",
            "zones_within_board",
        )
    ):
        return "manufacturing_violation"
    if any(token in name for token in ("route", "unrouted", "critical_nets_exist")):
        return "routing_congestion"
    if any(
        token in name
        for token in (
            "has_blocks",
            "has_nets",
            "has_ground",
            "has_supply",
            "has_board_outline",
            "has_net_classes",
            "requirement_text_present",
        )
    ):
        return "constraint_violation"
    if any(
        token in name
        for token in (
            "written",
            "available",
            "embedded",
            "round_trip",
            "labels_match",
        )
    ):
        return "harness_defect"
    return "erc_violation"


CHECK_FAILURE_CLASS: dict[str, str] = {
    name: _classify(name) for name in sorted(_discover_check_names())
}

if not set(CHECK_FAILURE_CLASS.values()) <= _LEGAL_CLASSES:  # pragma: no cover
    raise RuntimeError("Pipeline check taxonomy contains an illegal failure class")


CLASS_REPAIR_DIRECTIVE: dict[str, str] = {
    "constraint_violation": "Do not relax the user or datasheet constraint; report it.",
    "missing_component": "Add or replace only the missing selected physical component.",
    "missing_support_network": "Add the required support part and its explicit nets.",
    "symbol_unavailable": "Use a grounded installed symbol or request an approved substitute.",
    "symbol_mismatch": "Select a symbol matching the exact requested device identity.",
    "footprint_mismatch": "Select a footprint whose pads match the symbol pin numbers.",
    "pin_conflict": "Reassign each physical pin to at most one logical net.",
    "erc_violation": "Correct only the flagged schematic connectivity objects.",
    "tool_unavailable": "Record the capability gap; do not invent a tool result.",
    "transient_external_failure": (
        "Retry the same bounded operation without changing design intent."
    ),
    "routing_congestion": "Adjust placement or routing while preserving critical constraints.",
    "manufacturing_violation": "Correct the flagged geometry to the declared fab rules.",
    "harness_defect": "Stop and record a Harness capability gap.",
}


def failure_class_for(name: str) -> str | None:
    normalized = base_name(name)
    if normalized.startswith("tool_unavailable."):
        return "tool_unavailable"
    return CHECK_FAILURE_CLASS.get(normalized)


def repair_directives(names: Iterable[str]) -> list[str]:
    directives: list[str] = []
    for name in names:
        failure_class = failure_class_for(name)
        directive = CLASS_REPAIR_DIRECTIVE.get(failure_class or "")
        if directive and directive not in directives:
            directives.append(directive)
    return directives


__all__ = [
    "CHECK_FAILURE_CLASS",
    "CLASS_REPAIR_DIRECTIVE",
    "failure_class_for",
    "repair_directives",
]
