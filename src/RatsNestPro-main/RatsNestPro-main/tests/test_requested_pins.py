"""A pin the requirement names must appear in the finished pin map.

The defect: a requirement said the status LED is on ``PC13``. The finished board
never mentioned ``PC13`` at all — the LED sat between 3V3 and ground through a
resistor, permanently lit and not under software control. Every check passed,
because each one asked whether the design was internally consistent and none
asked whether it did what was requested.

The extraction is a regex over prose, so it will pick up tokens that look like
pins and are not. What makes that harmless is the intersection with the pins the
selected part actually has: a demand can only be raised for an identifier the
device really offers. That is the difference from
``led_current_limit_in_series``, which filtered candidate resistors on a ``role``
substring and so could never find any once the model named the part something
else.
"""

from __future__ import annotations

import pytest

from ratsnestpro.domain.contracts import RequirementSpec, Severity
from ratsnestpro.orchestration.pipeline import (
    PipelineState,
    PipelineStep,
    _requested_pin_checks,
    _requested_pin_names,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    MappedNet,
    MappedPin,
    PinMapPlan,
    SelectedPart,
    SelectionPlan,
)

_MCU = "MCU_ST_STM32F1:STM32F103C8Tx"


def _state(requirement: str, constraints: list[str] | None = None) -> PipelineState:
    state = PipelineState(requirement_text=requirement)
    state.artifacts[PipelineStep.REQUIREMENTS] = RequirementSpec(
        raw_text=requirement, constraints=constraints or []
    )
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(ref="U1", symbol=_MCU, value="STM32F103C8Tx", role=""),
    ])
    return state


def _plan(*pins: tuple[str, str]) -> PinMapPlan:
    return PinMapPlan(nets=[
        MappedNet(
            name=f"NET_{number}",
            kind="signal",
            pins=[MappedPin(ref="U1", logical=logical, number=number)],
        )
        for logical, number in pins
    ])


def _requires_library() -> None:
    from ratsnestpro.eda import symbols

    if not symbols.symbol_pins(_MCU):
        pytest.skip("MCU_ST_STM32F1 not in the installed symbol library")


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Status LED on PC13, button on PA0", {"PC13", "PA0"}),
        ("use GPIO2 for the LED", {"GPIO2"}),
        ("drive GP15 high", {"GP15"}),
        ("lower case pc13 counts too", {"PC13"}),
        # No identifier: a sentence about GPIO in general demands nothing.
        ("expose several GPIO on the header", set()),
        ("no pins here at all", set()),
        # Port letters stop at L, so PZ1 is not a port bit in any family modelled.
        ("a stray PZ1", set()),
    ],
)
def test_extraction(text: str, expected: set[str]) -> None:
    assert _requested_pin_names(text, []) == expected


def test_constraints_are_read_as_well_as_the_prose() -> None:
    """Reading only the constraints would depend on a model having restated it."""
    assert _requested_pin_names("a board", ["status LED must be on PC13"]) == {"PC13"}


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #


def test_requested_pin_absent_from_the_map_is_an_error() -> None:
    _requires_library()
    checks = _requested_pin_checks(
        _state("Status LED on PC13. User button on PA0."),
        _plan(("PA0", "10")),
    )
    by_name = {c.name: c for c in checks}
    assert by_name["requested_pin_used:PA0"].ok
    missing = by_name["requested_pin_used:PC13"]
    assert not missing.ok
    assert missing.severity is Severity.ERROR
    assert missing.failure_class == "erc_violation"
    # The message names where the pin is, so the repair does not have to look it up.
    assert "U1:2" in missing.message
    assert missing.targets == ["U1:2"]


def test_all_requested_pins_present_passes() -> None:
    _requires_library()
    checks = _requested_pin_checks(
        _state("Status LED on PC13. User button on PA0."),
        _plan(("PA0", "10"), ("PC13", "2")),
    )
    assert all(c.ok for c in checks)
    assert {c.name for c in checks} == {
        "requested_pin_used:PA0",
        "requested_pin_used:PC13",
    }


def test_a_requirement_naming_no_pin_produces_no_check() -> None:
    """Silence, not a pass: there is nothing to be right or wrong about."""
    assert _requested_pin_checks(_state("a simple STM32 board"), _plan()) == []


def test_an_identifier_the_device_does_not_have_is_dropped() -> None:
    """This is what makes a loose regex safe.

    ``PB99`` matches the pattern and is then discarded because the symbol has no
    such pin — so a false extraction cannot turn into a false demand.
    """
    _requires_library()
    assert _requested_pin_checks(
        _state("connect PB99 to the header"), _plan()
    ) == []


def test_no_selection_yet_produces_no_check() -> None:
    """Before parts exist there is no pin set to intersect with."""
    state = PipelineState(requirement_text="Status LED on PC13")
    assert _requested_pin_checks(state, _plan()) == []


def test_alternate_names_are_matched_too() -> None:
    """A requirement may name a peripheral function rather than a port bit."""
    _requires_library()
    from ratsnestpro.eda import symbols

    pins = symbols.symbol_pins(_MCU) or []
    alternates = {
        str(a).upper() for p in pins for a in (p.get("alternates") or ())
    }
    # Pick one the extraction pattern can actually produce.
    candidates = sorted(a for a in alternates if _requested_pin_names(a, []))
    if not candidates:
        pytest.skip("no alternate on this symbol matches the pin-identifier pattern")
    checks = _requested_pin_checks(_state(f"use {candidates[0]}"), _plan())
    assert [c.name for c in checks] == [f"requested_pin_used:{candidates[0]}"]


def test_pin_on_a_different_part_still_counts_as_used() -> None:
    """The demand is that the identifier is connected somewhere it exists."""
    _requires_library()
    state = _state("Status LED on PC13")
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(ref="U1", symbol=_MCU, value="STM32F103C8Tx", role=""),
        SelectedPart(ref="U9", symbol=_MCU, value="STM32F103C8Tx", role=""),
    ])
    plan = PinMapPlan(nets=[
        MappedNet(name="LED", kind="signal", pins=[
            MappedPin(ref="U9", logical="PC13", number="2"),
        ]),
    ])
    checks = _requested_pin_checks(state, plan)
    assert [c.ok for c in checks] == [True]
