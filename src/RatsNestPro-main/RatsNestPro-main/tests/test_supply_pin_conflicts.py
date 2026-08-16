"""A supply pin must not sit on the net that feeds its own regulator.

The defect is real and was shipped: ``ratsnest-370639d2`` wired the STM32F103's
``VBAT`` to ``/REG_IN`` alongside the AMS1117's ``VI``, so a part rated to 3.6 V
sat on unregulated USB ``VBUS`` behind a ferrite bead. The rule itself is pinned
down here against constructed inputs, including the designs it must stay silent
on; the end-to-end tests at the bottom read that run's real schematic and sweep
the demo corpus.

Silence matters more than detection here. The demo corpus contains exactly one
project any of the 17 fact sheets recognises, and that project has no regulator,
so the corpus cannot demonstrate a false-positive rate for this check at all.
The negative samples below are the only evidence there is.
"""

from __future__ import annotations

import pytest

from ratsnestpro.domain.contracts import Severity
from ratsnestpro.eda.factgate import resolve_sheets, supply_pin_conflicts
from ratsnestpro.orchestration.check_classes import failure_class_for
from ratsnestpro.orchestration.pipeline import (
    _ConnectivityView,
    _mcu_supply_source_checks,
)
from ratsnestpro.orchestration.pipeline_contracts import SelectedPart
from tests.fixtures import kicad_demos as demos

# Pin tables as a symbol library reports them: number, name, electrical type.
# Taken from the real symbols, which is why the LDO's ground pin is ``power_in``
# just like its supply input — the property that makes name-based ground
# detection necessary.
_STM32_PINS = [
    {"number": "1", "name": "VBAT", "type": "power_in"},
    {"number": "8", "name": "VSSA", "type": "power_in"},
    {"number": "9", "name": "VDDA", "type": "power_in"},
    {"number": "23", "name": "VSS", "type": "power_in"},
    {"number": "24", "name": "VDD", "type": "power_in"},
    {"number": "48", "name": "VDD", "type": "power_in"},
]
_AMS1117_PINS = [
    {"number": "1", "name": "GND", "type": "power_in"},
    {"number": "2", "name": "VO", "type": "power_out"},
    {"number": "3", "name": "VI", "type": "power_in"},
]
# TPS563201, a buck: no ``power_out`` pin exists at all. Its output is downstream
# of the inductor and ``SW`` is typed ``output``.
_TPS563201_PINS = [
    {"number": "1", "name": "GND", "type": "power_in"},
    {"number": "2", "name": "SW", "type": "output"},
    {"number": "3", "name": "VIN", "type": "power_in"},
    {"number": "4", "name": "VFB", "type": "input"},
    {"number": "5", "name": "EN", "type": "input"},
    {"number": "6", "name": "VBST", "type": "passive"},
]


def _mcu(ref: str = "U1") -> SelectedPart:
    return SelectedPart(
        ref=ref,
        symbol="MCU_ST_STM32F1:STM32F103C8Tx",
        value="STM32F103C8Tx",
        # Deliberately empty: identity must come from the fact sheet. A role of
        # "mcu" here would let the check pass for the wrong reason.
        role="",
    )


def _ldo(ref: str = "U2") -> SelectedPart:
    return SelectedPart(
        ref=ref, symbol="Regulator_Linear:AMS1117-3.3", value="AMS1117-3.3", role=""
    )


def _buck(ref: str = "U3") -> SelectedPart:
    return SelectedPart(
        ref=ref, symbol="Regulator_Switching:TPS563201", value="TPS563201", role=""
    )


# --------------------------------------------------------------------------- #
# Positive: the shipped defect
# --------------------------------------------------------------------------- #


def test_supply_pin_on_the_regulator_input_net_is_an_error() -> None:
    pins = {"U1": _STM32_PINS, "U2": _AMS1117_PINS}
    pin_nets = {
        ("U1", "1"): "/REG_IN",      # VBAT — the defect
        ("U1", "8"): "/GND",
        ("U1", "9"): "/VDDA_SUPPLY",
        ("U1", "23"): "/GND",
        ("U1", "24"): "/VDD33",
        ("U1", "48"): "/VDD33",
        ("U2", "1"): "/GND",
        ("U2", "2"): "/VDD33",       # VO
        ("U2", "3"): "/REG_IN",      # VI
    }
    findings = supply_pin_conflicts(
        [_mcu(), _ldo()], pin_nets=pin_nets, pins=pins
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.ref == "U1"
    assert finding.device == "STM32F103"
    assert finding.slot == "supply_range"
    # ``supply_range`` declares consequence ``burn``, so this blocks.
    assert finding.severity is Severity.ERROR
    assert "VBAT" in finding.message
    assert "/REG_IN" in finding.message
    assert "3.6 V" in finding.message
    # The repair target is named, which is what makes the finding actionable.
    assert "Move the pin to /VDD33" in finding.message
    # Provenance is the page that states the limit, not the sheet header.
    assert "Table 9" in finding.citation


def test_several_offending_pins_report_once_per_regulator() -> None:
    """One finding names every offending pin; three findings would be three repairs."""
    pins = {"U1": _STM32_PINS, "U2": _AMS1117_PINS}
    pin_nets = {
        ("U1", "1"): "/REG_IN",
        ("U1", "9"): "/REG_IN",
        ("U1", "24"): "/VDD33",
        ("U2", "2"): "/VDD33",
        ("U2", "3"): "/REG_IN",
    }
    findings = supply_pin_conflicts(
        [_mcu(), _ldo()], pin_nets=pin_nets, pins=pins
    )
    assert len(findings) == 1
    assert "U1:1 (VBAT)" in findings[0].message
    assert "U1:9 (VDDA)" in findings[0].message


# --------------------------------------------------------------------------- #
# Negative: designs that must stay silent
# --------------------------------------------------------------------------- #


def test_correctly_wired_supply_is_silent() -> None:
    pins = {"U1": _STM32_PINS, "U2": _AMS1117_PINS}
    pin_nets = {
        ("U1", "1"): "/VDD33",       # VBAT on the regulated rail — correct
        ("U1", "23"): "/GND",
        ("U1", "24"): "/VDD33",
        ("U2", "1"): "/GND",
        ("U2", "2"): "/VDD33",
        ("U2", "3"): "/VBUS",
    }
    assert not supply_pin_conflicts(
        [_mcu(), _ldo()], pin_nets=pin_nets, pins=pins
    )


def test_ground_pins_are_not_read_as_supply() -> None:
    """The LDO's ground pin is ``power_in``; so are the MCU's ``VSS`` and ``VSSA``.

    Without name-based ground detection ``/GND`` would count as one of the
    regulator's input nets, and every grounded MCU would be reported.
    """
    pins = {"U1": _STM32_PINS, "U2": _AMS1117_PINS}
    pin_nets = {
        ("U1", "8"): "/GND",
        ("U1", "23"): "/GND",
        ("U1", "24"): "/VDD33",
        ("U2", "1"): "/GND",
        ("U2", "2"): "/VDD33",
        ("U2", "3"): "/VBUS",
    }
    assert not supply_pin_conflicts(
        [_mcu(), _ldo()], pin_nets=pin_nets, pins=pins
    )


def test_two_stage_supply_is_silent() -> None:
    """A rail that is one regulator's output and the next one's input is legal.

    5 V -> U2 -> 3.3 V feeds the MCU's VDD; 3.3 V -> U4 -> the MCU's analog rail.
    ``/VDD33`` is therefore simultaneously a supply the MCU sits on and U4's
    input. Excluding regulator-driven nets is what keeps this quiet.
    """
    pins = {"U1": _STM32_PINS, "U2": _AMS1117_PINS, "U4": _AMS1117_PINS}
    pin_nets = {
        ("U1", "24"): "/VDD33",
        ("U1", "9"): "/VDDA_SUPPLY",
        ("U2", "2"): "/VDD33",
        ("U2", "3"): "/VBUS",
        ("U4", "2"): "/VDDA_SUPPLY",
        ("U4", "3"): "/VDD33",
    }
    assert not supply_pin_conflicts(
        [_mcu(), _ldo("U2"), _ldo("U4")], pin_nets=pin_nets, pins=pins
    )


def test_regulator_that_does_not_feed_this_device_is_ignored() -> None:
    """A second rail the MCU never touches cannot implicate it."""
    pins = {"U1": _STM32_PINS, "U2": _AMS1117_PINS}
    pin_nets = {
        ("U1", "1"): "/SENSOR_5V",   # on the LDO's input, but...
        ("U1", "24"): "/VDD33",
        ("U2", "2"): "/SENSOR_3V3",  # ...its output goes nowhere near the MCU
        ("U2", "3"): "/SENSOR_5V",
    }
    assert not supply_pin_conflicts(
        [_mcu(), _ldo()], pin_nets=pin_nets, pins=pins
    )


def test_buck_converter_yields_no_verdict() -> None:
    """No ``power_out`` pin means no identifiable output net — so no claim.

    Guessing that ``SW`` is the output would be wrong: the regulated rail is on
    the far side of the inductor.
    """
    pins = {"U1": _STM32_PINS, "U3": _TPS563201_PINS}
    pin_nets = {
        ("U1", "1"): "/VIN_5V",
        ("U1", "24"): "/VDD33",
        ("U3", "1"): "/GND",
        ("U3", "2"): "/SW_NODE",
        ("U3", "3"): "/VIN_5V",
    }
    assert not supply_pin_conflicts(
        [_mcu(), _buck()], pin_nets=pin_nets, pins=pins
    )


def test_unknown_device_is_not_judged() -> None:
    """No fact sheet on either side means no verdict, not a guessed limit."""
    pins = {"U1": _STM32_PINS, "U9": _AMS1117_PINS}
    pin_nets = {
        ("U1", "1"): "/REG_IN",
        ("U1", "24"): "/VDD33",
        ("U9", "2"): "/VDD33",
        ("U9", "3"): "/REG_IN",
    }
    unknown_regulator = SelectedPart(
        ref="U9", symbol="Regulator_Linear:XC6206P332MR", value="XC6206P332MR", role=""
    )
    assert not supply_pin_conflicts(
        [_mcu(), unknown_regulator], pin_nets=pin_nets, pins=pins
    )
    unknown_mcu = SelectedPart(
        ref="U1", symbol="MCU_Microchip_PIC16:PIC16F54-ISO", value="PIC16F54", role=""
    )
    assert not supply_pin_conflicts(
        [unknown_mcu, _ldo("U9")], pin_nets=pin_nets, pins=pins
    )


def test_role_text_cannot_create_or_suppress_a_finding() -> None:
    """Identity comes from the fact sheet; ``role`` is free text a model wrote."""
    pins = {"U1": _STM32_PINS, "U2": _AMS1117_PINS}
    pin_nets = {
        ("U1", "1"): "/REG_IN",
        ("U1", "24"): "/VDD33",
        ("U2", "2"): "/VDD33",
        ("U2", "3"): "/REG_IN",
    }
    misleading_mcu = SelectedPart(
        ref="U1",
        symbol="MCU_ST_STM32F1:STM32F103C8Tx",
        value="STM32F103C8Tx",
        role="decoupling capacitor",
    )
    misleading_ldo = SelectedPart(
        ref="U2",
        symbol="Regulator_Linear:AMS1117-3.3",
        value="AMS1117-3.3",
        role="crystal",
    )
    assert len(
        supply_pin_conflicts(
            [misleading_mcu, misleading_ldo], pin_nets=pin_nets, pins=pins
        )
    ) == 1


def test_unwired_pins_make_no_claim() -> None:
    """A pin with no net is not evidence of anything, in either direction."""
    pins = {"U1": _STM32_PINS, "U2": _AMS1117_PINS}
    assert not supply_pin_conflicts([_mcu(), _ldo()], pin_nets={}, pins=pins)


def test_no_regulator_means_no_work() -> None:
    pins = {"U1": _STM32_PINS}
    pin_nets = {("U1", "1"): "/VDD33", ("U1", "24"): "/VDD33"}
    assert not supply_pin_conflicts([_mcu()], pin_nets=pin_nets, pins=pins)


def test_missing_pin_table_is_silent_rather_than_raising() -> None:
    """Symbol pins can be unavailable; that must not crash a check run."""
    pin_nets = {("U1", "1"): "/REG_IN", ("U2", "3"): "/REG_IN"}
    assert not supply_pin_conflicts([_mcu(), _ldo()], pin_nets=pin_nets, pins={})



# --------------------------------------------------------------------------- #
# End to end: the real schematic that shipped, and the corpus
# --------------------------------------------------------------------------- #


@demos.requires_positive_sample
@demos.requires_kicad_cli
def test_shipped_run_reproduces_the_defect() -> None:
    """Read the run's own ``.kicad_sch`` and find what review missed.

    Nothing is constructed here: the connectivity comes from KiCad's netlister
    and the identities from the fact sheets, so this is the check running exactly
    as the pipeline runs it.
    """
    run = demos.positive_sample_run()
    assert run is not None
    sheet = run / "stm32f103c8t6-board.kicad_sch"
    view = _ConnectivityView.from_schematic(sheet)

    # The evidence, stated independently of the check under test.
    assert view.pin_nets[("U1", "1")] == "/REG_IN"    # STM32 VBAT
    assert view.pin_nets[("U2", "3")] == "/REG_IN"    # AMS1117 VI
    assert view.pin_nets[("U2", "2")] == "/VDD33"     # AMS1117 VO
    assert view.pin_nets[("U1", "24")] == "/VDD33"    # STM32 VDD

    checks = _mcu_supply_source_checks(view)
    assert [c.name for c in checks] == ["supply_pin_not_on_regulator_input:U1"]
    assert checks[0].severity is Severity.ERROR
    assert "VBAT" in checks[0].message


@demos.requires_positive_sample
@demos.requires_kicad_cli
def test_finding_is_classified_as_rewirable() -> None:
    """The repair is a wire move, so the class must be the one that repairs wires."""
    assert (
        failure_class_for("supply_pin_not_on_regulator_input:U1") == "erc_violation"
    )


@pytest.mark.real_kicad
def test_demo_corpus_reports_nothing() -> None:
    """False-positive floor on known-good designs — weak evidence, stated as such.

    Exactly one demo project matches any of the 17 fact sheets (``tiny_tapeout``,
    an MCU with no regulator), so a clean sweep here says almost nothing about
    this check. It is recorded to catch the opposite failure: a change that makes
    the check fire on designs it has no business judging.
    """
    matched = 0
    for path, _netlist in demos.demo_netlists():
        view = _ConnectivityView.from_schematic(path)
        parts = list(view.parts.values())
        if resolve_sheets(parts):
            matched += 1
        assert not _mcu_supply_source_checks(view), f"fired on {path.parent.name}"
    assert matched >= 1, "no demo project resolves to a fact sheet; scan broken"
