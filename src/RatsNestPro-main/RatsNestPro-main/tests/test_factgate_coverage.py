"""Task 0b — the coverage signal at the fact-sheet BOUNDARY.

``factsheet`` uses a four-state ``Status`` to make a blank cell inside a sheet
unrepresentable. That discipline stops at the boundary: ``slot_verdicts`` skips a
part with no sheet, and a skipped part looks exactly like a clean one. Failing
open there is deliberate and must stay — this project selects parts open-world,
so blocking every device outside the roster would break it. What these tests pin
is the ANNOUNCEMENT: an uncovered part must say so, and must keep not blocking.
"""

from __future__ import annotations

from dataclasses import dataclass

from ratsnestpro.domain.contracts import Severity
from ratsnestpro.eda import factgate
from ratsnestpro.eda.factsheet import (
    QUESTIONNAIRE,
    SLOT_SPECS,
    Comparison,
    Consequence,
    DeviceClass,
)


@dataclass
class _Part:
    ref: str
    value: str
    role: str
    symbol: str = ""
    footprint: str = ""


# --------------------------------------------------------------------------- #
# What gets reported
# --------------------------------------------------------------------------- #


def test_uncovered_mcu_is_reported_with_its_unchecked_gates() -> None:
    gaps = factgate.coverage_gaps([_Part("U1", "STM32F407VGT6", "mcu")])
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.ref == "U1"
    assert gap.device_class is DeviceClass.MCU
    # Only gateable slots count: a data-only slot never produced a verdict even
    # for a covered part, so counting it would overstate what was lost.
    expected = {
        slot for slot in QUESTIONNAIRE[DeviceClass.MCU]
        if SLOT_SPECS[slot].comparison is not Comparison.NONE
    }
    assert set(gap.unchecked_gates) == expected
    assert gap.severe_gates, "an MCU has burn/malfunction gates"
    assert "no datasheet fact sheet" in gap.as_text()
    assert "absence of evidence" in gap.as_text().lower()


def test_covered_part_produces_no_gap() -> None:
    assert factgate.coverage_gaps([_Part("U1", "STM32F103C8T6", "mcu")]) == []
    assert factgate.coverage_gaps([_Part("U2", "AMS1117-3.3", "ldo regulator")]) == []
    assert factgate.coverage_gaps([_Part("Y1", "X322512MSB4SI", "crystal")]) == []


def test_esp32_s3_is_now_a_coverage_gap_not_a_wrong_verdict() -> None:
    """The regression this whole phase exists for.

    Before the matcher was tightened, an ESP32-S3 resolved to the classic ESP32
    sheet and produced a page-cited ``pin_count`` ERROR against the wrong
    silicon. It must now be reported as missing coverage instead.
    """
    part = _Part("U1", "ESP32-S3", "mcu")
    gaps = factgate.coverage_gaps([part])
    assert len(gaps) == 1 and gaps[0].device_class is DeviceClass.MCU
    assert factgate.slot_verdicts([part], factgate.DesignObservation()) == []


def test_regulator_class_is_split_by_role() -> None:
    ldo = factgate.coverage_gaps([_Part("U2", "TLV70233", "ldo regulator")])
    dcdc = factgate.coverage_gaps([_Part("U3", "MP1584EN", "buck converter")])
    assert ldo[0].device_class is DeviceClass.LDO
    assert dcdc[0].device_class is DeviceClass.DCDC


# --------------------------------------------------------------------------- #
# What must NOT be reported — the signal has to stay findable
# --------------------------------------------------------------------------- #


def test_passives_and_mechanical_parts_are_not_reported() -> None:
    """A resistor has no questionnaire; flagging every one would bury the signal."""
    quiet = [
        _Part("R1", "10k", "pullup"),
        _Part("C1", "100nF", "mcu_vdd_decoupling_1"),
        _Part("D1", "LED red", "power_led"),
        _Part("H1", "MountingHole", "mounting_hole"),
        _Part("SW1", "SW_Push", "reset_button"),
    ]
    assert factgate.coverage_gaps(quiet) == []


def test_plain_pin_header_is_not_reported_but_usb_receptacle_is() -> None:
    """Only a bus with modelled electrical limits earns a connector report."""
    assert factgate.coverage_gaps([_Part("J9", "Conn_01x08", "breakout_header")]) == []
    usb = factgate.coverage_gaps([_Part("J1", "USB4110-GF-A", "usb_c_connector")])
    assert len(usb) == 1 and usb[0].device_class is DeviceClass.CONNECTOR


# --------------------------------------------------------------------------- #
# The invariant that must survive: reporting a gap is not blocking on one
# --------------------------------------------------------------------------- #


def test_coverage_reporting_does_not_change_fail_open_gating() -> None:
    parts = [_Part("U1", "STM32F407VGT6", "mcu"), _Part("U2", "TLV70233", "ldo")]
    assert factgate.gate_findings(parts, rails=["5V", "3V3"]) == []


def test_gap_severity_never_exceeds_warning_in_the_pipeline() -> None:
    """An absence of evidence must not be dressed up as a proven violation."""
    from ratsnestpro.orchestration.pipeline import PipelineState, SelectionStep
    from ratsnestpro.orchestration.pipeline_contracts import SelectedPart, SelectionPlan

    plan = SelectionPlan(
        parts=[
            SelectedPart(
                ref="U1", symbol="MCU_ST_STM32F4:STM32F407VGTx",
                value="STM32F407VGT6", footprint="", role="mcu",
            )
        ],
        rationale="test",
    )
    checks = SelectionStep().check(PipelineState(requirement_text="STM32F407 board"), plan)
    coverage = [c for c in checks if c.name.startswith("datasheet_coverage")]
    assert coverage, "an uncovered MCU must be announced"
    assert all(c.severity is Severity.WARNING for c in coverage)
    assert not any(
        not c.ok and c.severity is Severity.ERROR
        and c.name.startswith("datasheet_coverage")
        for c in checks
    )


def test_severe_gates_exclude_margin_slots() -> None:
    gaps = factgate.coverage_gaps([_Part("U2", "TLV70233", "ldo regulator")])
    severe = set(gaps[0].severe_gates)
    margin = {
        slot for slot in gaps[0].unchecked_gates
        if SLOT_SPECS[slot].consequence is Consequence.MARGIN
    }
    assert margin, "the LDO questionnaire has at least one margin gate (required_cin)"
    assert not (severe & margin)


# --------------------------------------------------------------------------- #
# A satellite passive is not its device
# --------------------------------------------------------------------------- #


def test_decoupling_cap_does_not_supply_the_package_selector() -> None:
    """Roles read "<device>_<function>", so "mcu_vdd_decoupling_1" matches "mcu".

    Before the support-part guard the first such capacitor in the part list set
    ``ctx["package"]`` from its own 0402 footprint, and every ConditionalFact
    keyed on ``package`` resolved against a capacitor instead of the MCU.
    """
    parts = [
        _Part("C1", "100nF", "mcu_vdd_decoupling_1", footprint="Capacitor_SMD:C_0402_1005Metric"),
        _Part("U1", "ATmega328P-AU", "mcu", footprint="Package_QFP:TQFP-32_7x7mm_P0.8mm"),
    ]
    obs = factgate.observe(parts, rails=["5V"])
    assert obs.context.get("package") == "TQFP-32_7x7mm_P0.8mm"


def test_buck_support_parts_are_not_classified_as_converters() -> None:
    support = [
        _Part("C10", "22uF", "buck_input_cap"),
        _Part("C11", "22uF", "buck_output_cap"),
        _Part("L1", "2.2uH", "buck_inductor"),
        _Part("C12", "100nF", "buck_bootstrap_cap"),
    ]
    assert factgate.coverage_gaps(support) == []
