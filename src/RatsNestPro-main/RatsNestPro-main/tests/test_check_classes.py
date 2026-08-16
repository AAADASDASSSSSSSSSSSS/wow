"""Every bottom-line check declares a failure class, and the class is legal.

The point of the declaration table is that a gap is *detectable*. These tests are
that detector: a new ERROR-severity check with no entry fails
:func:`test_every_error_check_declares_a_class` instead of silently reaching the
AHE loop as ``unknown`` and being answered by whichever regex happens to match
its wording.

The names are harvested from the pipeline source rather than by executing every
step, because most checks only appear under a specific artifact shape and a
runtime sweep would miss exactly the rare ones most likely to be forgotten.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ratsnestpro.orchestration import check_classes, pipeline
from ratsnestpro.orchestration.check_classes import (
    CHECK_FAILURE_CLASS,
    base_name,
    failure_class_for,
)

# The taxonomy lives on the diagnosis side; import it if that package is
# reachable, otherwise fall back to the literal set so this file still guards the
# table's internal consistency.
try:
    from agents.ratsnestpro.diagnosis import _REPAIR_SCOPE, _STRATEGY

    _HAVE_DIAGNOSIS = True
except Exception:  # pragma: no cover - agents package not on the path
    _STRATEGY = {}
    _REPAIR_SCOPE = {}
    _HAVE_DIAGNOSIS = False

_LEGAL_CLASSES = {
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
    "unknown",
}


def _error_check_names() -> set[str]:
    """Base names of every ERROR-severity CheckResult built by the pipeline.

    A check is WARNING-severity only when ``Severity.WARNING`` appears inside its
    own constructor call, so the scan reads each ``CheckResult(`` block rather
    than the whole line.
    """
    source = Path(pipeline.__file__).read_text(encoding="utf-8")
    names: set[str] = set()
    for match in re.finditer(r"CheckResult\(", source):
        blob = source[match.end() : match.end() + 700]
        # Stop at the next CheckResult so severities cannot leak across calls.
        nxt = blob.find("CheckResult(")
        if nxt != -1:
            blob = blob[:nxt]
        if "Severity.WARNING" in blob:
            continue
        literal = re.search(r'name=f?"([a-z0-9_.]+)', blob)
        if literal:
            names.add(base_name(literal.group(1)))
    return {n for n in names if n}


def test_scan_finds_a_plausible_number_of_checks() -> None:
    """Guard the harvester itself: a broken scan would make the table look complete."""
    found = _error_check_names()
    assert len(found) >= 60, f"only {len(found)} ERROR checks harvested — scan likely broken"


def test_every_error_check_declares_a_class() -> None:
    undeclared = sorted(n for n in _error_check_names() if failure_class_for(n) is None)
    assert not undeclared, (
        "these ERROR-severity checks have no entry in CHECK_FAILURE_CLASS:\n  "
        + "\n  ".join(undeclared)
        + "\n\nAdd each one with the class whose repair strategy actually fixes it."
    )


def test_declared_classes_are_legal() -> None:
    illegal = {n: c for n, c in CHECK_FAILURE_CLASS.items() if c not in _LEGAL_CLASSES}
    assert not illegal, f"unknown failure classes in the table: {illegal}"


def test_table_has_no_stale_entries() -> None:
    """An entry for a check that no longer exists is dead weight and misleading."""
    known = _error_check_names()
    # WARNING-severity checks are legitimately mapped too (they still reach the
    # report), so only flag entries matching nothing in the source at all.
    source = Path(pipeline.__file__).read_text(encoding="utf-8")
    stale = [
        name
        for name in CHECK_FAILURE_CLASS
        if name not in known and f'"{name}' not in source
    ]
    assert not stale, f"table entries with no matching check: {stale}"


def test_no_class_maps_to_unknown() -> None:
    """Declaring ``unknown`` would be worse than declaring nothing."""
    assert "unknown" not in set(CHECK_FAILURE_CLASS.values())


@pytest.mark.skipif(not _HAVE_DIAGNOSIS, reason="agents package not importable")
def test_every_declared_class_has_a_strategy_and_scope() -> None:
    for name, cls in CHECK_FAILURE_CLASS.items():
        assert cls in _STRATEGY, f"{name}: class {cls} has no repair strategy"
        assert cls in _REPAIR_SCOPE, f"{name}: class {cls} has no repair scope entry"


def test_environment_probes_classify_without_a_table_entry() -> None:
    """Probe checks are generated per dependency, so the prefix carries the class."""
    assert failure_class_for("tool_unavailable.symbol_library") == "tool_unavailable"
    assert failure_class_for("tool_unavailable.freerouting") == "tool_unavailable"


def test_reference_suffix_is_ignored() -> None:
    """Several checks append a component reference; the class must still resolve."""
    assert failure_class_for("two_terminal_not_shorted:C10") == "erc_violation"
    assert failure_class_for("footprint:U1") == "footprint_mismatch"
    assert base_name("crystal_two_distinct_signal_nets:Y1") == (
        "crystal_two_distinct_signal_nets"
    )


def test_unmapped_name_returns_none_not_a_guess() -> None:
    assert failure_class_for("something_never_written") is None
    assert failure_class_for("") is None


def test_datasheet_limit_stand_ins_are_split_by_repairability() -> None:
    """The documented gap: selection-time limits block, connection-time ones rewire."""
    assert CHECK_FAILURE_CLASS["datasheet_limits"] == "constraint_violation"
    assert CHECK_FAILURE_CLASS["input_voltage_rating"] == "constraint_violation"
    assert CHECK_FAILURE_CLASS["datasheet_connection"] == "erc_violation"


def test_excess_decoupling_does_not_map_to_add_parts() -> None:
    """Mapping an 'too many parts' failure to extend_selection would push the wrong way."""
    assert CHECK_FAILURE_CLASS["mcu_supply_decoupling_not_excessive"] != (
        "missing_support_network"
    )


def test_module_exports_are_stable() -> None:
    assert set(check_classes.__all__) == {
        "CHECK_FAILURE_CLASS",
        "CLASS_REPAIR_DIRECTIVE",
        "failure_class_for",
        "repair_directives",
    }
