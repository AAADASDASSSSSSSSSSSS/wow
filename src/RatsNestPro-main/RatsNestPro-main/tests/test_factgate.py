"""INF1 - the fact-sheet gate: does a real design get judged against real facts?

:mod:`ratsnestpro.tests.test_fact_sheets_data` proves the FACTS are right.
This file proves they are CONSUMED - that a design which provably violates a
cited datasheet figure is blocked, and that a legal design is not.

The distinction matters because the failure this module replaces was silent: the
legacy gate looked up one device (the MCU) and compared three hard-coded fields,
so sixteen of the seventeen sheets could not affect any outcome. A fact base
nobody reads is indistinguishable from no fact base at all, and no test caught
that. Every trap below is a mismatch found in real distributor stock or real
datasheets during extraction, so these tests fail if the wiring rots.
"""

from __future__ import annotations

import pytest

from ratsnestpro.domain.contracts import Severity
from ratsnestpro.eda import factgate
from ratsnestpro.eda.factgate import gate_findings, observe
from ratsnestpro.eda.factsheet import (
    QUESTIONNAIRE,
    SLOT_SPECS,
    Comparison,
    DeviceClass,
    fact_sheet,
)
from ratsnestpro.orchestration.pipeline_contracts import SelectedPart


def part(
    ref: str, value: str, role: str, footprint: str = "", symbol: str = "Device:U"
) -> SelectedPart:
    return SelectedPart(
        ref=ref, value=value, role=role, footprint=footprint, symbol=symbol
    )


def slots_flagged(findings: list[factgate.GateFinding]) -> set[str]:
    return {f.slot for f in findings}


# --------------------------------------------------------------------------- #
# Reading the design
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("rail", "expected"),
    [
        # Bare forms.
        ("3V3", 3.3), ("1V8", 1.8), ("5V", 5.0), ("3.3V", 3.3), ("24V", 24.0),
        ("5V0", 5.0), ("12V", 12.0),
        # Decorated forms that occur in this codebase. An anchored pattern
        # returned None for every one of these, and in a fail-open gate that
        # means the check silently stops existing.
        ("+3V3", 3.3), ("+3.3V", 3.3), ("VCORE_1V8", 1.8), ("Power_5V", 5.0),
        ("3V3_MCU", 3.3), ("VDD_3V3", 3.3),
        # Names that carry no voltage must yield None rather than a guess: a
        # fabricated rail voltage would gate against a number nobody stated.
        ("VBUS", None), ("VBAT", None), ("GND", None), ("", None),
        ("VCC", None), ("USB_DP", None), ("CAN1", None),
        # A part number must never be read as a rail voltage even though it
        # contains a voltage-shaped token.
        ("PESD5V0L1BA", None), ("USBLC6-2", None),
    ],
)
def test_rail_names_parse_or_abstain(rail: str, expected: float | None) -> None:
    assert factgate._rail_voltage(rail) == expected


def test_regulator_output_ignores_digits_inside_a_part_number() -> None:
    """'TPS563201' must not be read as 5.6 V or 3.2 V.

    The helper this replaces used a bare ``(\\d\\.\\d)`` search, which happens to
    work for AP2112K-3.3 and fails silently on part numbers whose digits are not
    a voltage.
    """
    assert factgate._regulator_output_v(part("U1", "AP2112K-3.3", "ldo")) == 3.3
    assert factgate._regulator_output_v(part("U1", "AMS1117-5.0", "ldo")) == 5.0
    assert factgate._regulator_output_v(part("U1", "TPS563201", "dcdc")) is None
    assert factgate._regulator_output_v(part("U1", "LP2985-3V3", "ldo")) == 3.3


def test_observation_is_empty_without_artifacts() -> None:
    """No parts and no rails means no observations - and therefore no verdicts."""
    obs = observe([])
    assert obs.logic_supply_v is None
    assert obs.clock_mhz is None
    assert obs.highest_rail_v is None
    assert gate_findings([]) == []


def test_unknown_parts_never_produce_findings() -> None:
    """A part with no fact sheet is invisible to the gate, not a violation."""
    parts = [
        part("U1", "SomeUnknownMCU-99", "mcu"),
        part("U2", "MysteryRegulator", "ldo regulator"),
    ]
    assert gate_findings(parts, rails=["48V"]) == []


# --------------------------------------------------------------------------- #
# The traps - each one found in real stock or real datasheets
# --------------------------------------------------------------------------- #

def test_atmega_at_16mhz_on_3v3_is_blocked() -> None:
    """The classic AVR speed-grade violation: 16 MHz needs at least 4.5 V."""
    parts = [
        part("U1", "ATmega328P", "mcu"),
        part("U2", "AP2112K-3.3", "ldo regulator"),
        part("Y1", "X322516MLB4SI 16MHz", "crystal"),
    ]
    findings = gate_findings(parts, rails=["3V3"])
    assert "freq_vs_supply" in slots_flagged(findings)
    hit = next(f for f in findings if f.slot == "freq_vs_supply")
    assert hit.severity is Severity.ERROR, "an over-clocked supply is burn class"
    assert "10" in hit.message, "the 2.7-5.5 V speed grade caps at 10 MHz"
    assert "Speed Grade" in hit.citation


def test_same_atmega_at_16mhz_on_5v_is_clean() -> None:
    """The gate must not flag a legal design - the mirror of the test above."""
    parts = [
        part("U1", "ATmega328P", "mcu"),
        part("U2", "AMS1117-5.0", "ldo regulator"),
        part("Y1", "X322516MLB4SI 16MHz", "crystal"),
    ]
    assert gate_findings(parts, rails=["5V"]) == []


def test_most_stocked_12mhz_crystal_is_blocked_on_rp2040() -> None:
    """80 ohm ESR against the RP2040's 50 ohm ceiling.

    This is the sharpest finding in the fact base: the cheapest, most stocked,
    JLCPCB Basic 12 MHz crystal is out of specification for the very MCU that
    mandates 12 MHz. It is only catchable because BOTH numbers are recorded -
    and because the 50 was promoted out of a prose note into a real field.
    """
    parts = [
        part("U1", "RP2040", "mcu"),
        part("U2", "AP2112K-3.3", "ldo regulator"),
        part("Y1", "X322512MSB4SI 12MHz", "crystal"),
    ]
    findings = gate_findings(parts, rails=["3V3"])
    hit = next((f for f in findings if f.slot == "esr_max_ohm"), None)
    assert hit is not None, "the ESR mismatch must be caught"
    assert hit.severity is Severity.ERROR
    assert "80" in hit.message and "50" in hit.message
    assert "RP2040" in hit.message


def test_stocked_40mhz_crystal_is_blocked_on_esp32_by_ppm_sum() -> None:
    """Tolerance and drift add: +/-10 plus +/-30 misses a +/-10 ppm requirement.

    The part is advertised as "+/-10 ppm", which is why this needs a gate rather
    than a reading of the marketing line.
    """
    parts = [
        part("U1", "ESP32-WROOM-32", "mcu"),
        part("U2", "AP2112K-3.3", "ldo regulator"),
        part("Y1", "TXM40M0004252HBCEO00T 40MHz", "crystal"),
    ]
    findings = gate_findings(parts, rails=["3V3"])
    hit = next((f for f in findings if f.slot == "frequency_tolerance_ppm"), None)
    assert hit is not None
    assert hit.severity is Severity.ERROR
    assert "40 ppm" in hit.message, "the sum, not either figure alone"
    assert "add" in hit.message.lower()


def test_wrong_mandated_crystal_frequency_is_blocked() -> None:
    """ESP32 firmware supports only 40 MHz; a 12 MHz part is not a substitution."""
    parts = [
        part("U1", "ESP32-WROOM-32", "mcu"),
        part("Y1", "X322512MSB4SI 12MHz", "crystal"),
    ]
    findings = gate_findings(parts, rails=["3V3"])
    assert "freq_mhz" in slots_flagged(findings)
    hit = next(f for f in findings if f.slot == "freq_mhz")
    assert "40" in hit.message and "12" in hit.message
    assert "ESP32" in hit.message and "X322512MSB4SI" in hit.message


def test_crystal_outside_an_hse_range_is_blocked() -> None:
    """A range requirement must be honoured as a range, not as a fixed value.

    STM32's HSE accepts 4-16 MHz. Treating that as "requires exactly 4 MHz"
    would reject every legal crystal, so the two fact shapes are handled apart.
    """
    parts = [
        part("U1", "STM32F103", "mcu"),
        part("Y1", "TXM40M0004252HBCEO00T 40MHz", "crystal"),
    ]
    findings = gate_findings(parts, rails=["3V3"])
    hit = next((f for f in findings if f.slot == "freq_mhz"), None)
    assert hit is not None
    assert "4-16" in hit.message and "40" in hit.message


def test_a_legal_hse_crystal_is_not_flagged() -> None:
    parts = [
        part("U1", "STM32F103", "mcu"),
        part("Y1", "X322516MLB4SI 16MHz", "crystal"),
    ]
    assert "freq_mhz" not in slots_flagged(gate_findings(parts, rails=["3V3"]))


def test_24v_into_a_6v_ldo_is_blocked_twice_over() -> None:
    """An industrial input feeding a 3.3 V LDO directly - the burn case.

    Both the recommended range and the absolute maximum must fire: they are
    separate slots precisely because some parts publish only one of them.
    """
    parts = [
        part("U1", "AP2112K-3.3", "ldo regulator"),
        part("U2", "ATmega328P", "mcu"),
    ]
    findings = gate_findings(parts, rails=["24V", "3V3"])
    flagged = slots_flagged(findings)
    assert {"vin_range", "abs_max_vin"} <= flagged
    for f in findings:
        if f.slot in ("vin_range", "abs_max_vin"):
            assert f.severity is Severity.ERROR, "over-voltage is burn class"
            assert "24" in f.message


def test_findings_cite_the_violated_fact_not_the_sheet_header() -> None:
    """Provenance must point at the fact, which the legacy gate got wrong.

    ``verify_clock_supply`` interpolated ``facts.source.ref`` - the sheet's own
    source - into every violation string, so a speed-grade violation was
    attributed to whatever document the sheet header happened to name. These two
    slots come from different pages of the same datasheet, and the citations must
    differ accordingly.
    """
    parts = [part("U1", "AP2112K-3.3", "ldo regulator")]
    findings = gate_findings(parts, rails=["24V", "3V3"])
    cites = {f.slot: f.citation for f in findings}
    assert "Recommended Operating Conditions" in cites["vin_range"]
    assert "Absolute Maximum" in cites["abs_max_vin"]
    assert cites["vin_range"] != cites["abs_max_vin"]


# --------------------------------------------------------------------------- #
# Netlist-dependent gates
# --------------------------------------------------------------------------- #

class _Pin:
    def __init__(self, ref: str) -> None:
        self.ref = ref


class _Net:
    def __init__(self, name: str, refs: list[str]) -> None:
        self.name = name
        self.pins = [_Pin(r) for r in refs]


class _Netlist:
    def __init__(self, nets: list[_Net]) -> None:
        self.nets = nets


def test_output_capacitor_is_not_gated_before_the_netlist_exists() -> None:
    """Attribution needs connectivity; guessing would be worse than silence.

    Without a netlist there is no way to tell an input capacitor from an output
    one, so ``required_cout`` must stay quiet rather than compare the datasheet
    minimum against an arbitrary capacitor.
    """
    parts = [
        part("U1", "AMS1117-3.3", "ldo regulator"),
        part("C1", "100nF", "decoupling"),
    ]
    obs = observe(parts, rails=["5V", "3V3"])
    assert obs.output_cap_uf == {}
    assert "required_cout" not in slots_flagged(gate_findings(parts, rails=["5V", "3V3"]))


def test_undersized_output_capacitor_is_blocked_once_the_netlist_exists() -> None:
    """AMS1117 requires 22 uF of solid tantalum for loop stability.

    A 1 uF ceramic - correct for an AP2112K - is not interchangeable here, and
    the netlist is what makes the capacitor attributable to the output node.
    """
    parts = [
        part("U1", "AMS1117-3.3", "ldo regulator"),
        part("C1", "1uF", "output capacitor"),
    ]
    netlist = _Netlist([_Net("5V", ["U1"]), _Net("3V3", ["U1", "C1"])])
    obs = observe(parts, rails=["5V", "3V3"], netlist=netlist)
    assert obs.output_cap_uf.get("U1") == pytest.approx(1.0)
    findings = gate_findings(parts, rails=["5V", "3V3"], netlist=netlist)
    hit = next((f for f in findings if f.slot == "required_cout"), None)
    assert hit is not None, "1 uF against a 22 uF requirement must be caught"
    assert "22" in hit.message


def test_parallel_capacitors_sum_on_the_node() -> None:
    """A datasheet minimum applies to the node, not to a single component."""
    parts = [
        part("U1", "AMS1117-3.3", "ldo regulator"),
        part("C1", "10uF", "output capacitor"),
        part("C2", "10uF", "output capacitor"),
        part("C3", "2.2uF", "output capacitor"),
    ]
    netlist = _Netlist([
        _Net("5V", ["U1"]),
        _Net("3V3", ["U1", "C1", "C2", "C3"]),
    ])
    obs = observe(parts, rails=["5V", "3V3"], netlist=netlist)
    assert obs.output_cap_uf.get("U1") == pytest.approx(22.2)
    findings = gate_findings(parts, rails=["5V", "3V3"], netlist=netlist)
    assert "required_cout" not in slots_flagged(findings), (
        "22.2 uF total satisfies the 22 uF minimum even though no single "
        "capacitor does"
    )


def test_wrong_cc_pulldown_is_blocked_but_a_tolerance_part_is_not() -> None:
    """Type-C Rd is 5.1 kOhm +/-20%, so 4.7 k is legal and 10 k is not.

    An exact-match slot without the asserted tolerance would reject the 4.7 k -
    the reason ``tolerance_pct`` was added to the contract.
    """
    def build(rd: str) -> tuple[list[SelectedPart], _Netlist]:
        parts = [
            part("J1", "USB-C 16P", "usb connector"),
            part("R1", rd, "cc pulldown"),
        ]
        return parts, _Netlist([_Net("CC1", ["J1", "R1"])])

    parts, netlist = build("4.7k")
    obs = observe(parts, netlist=netlist)
    assert obs.cc_pulldown_ohm.get("J1") == pytest.approx(4700.0)
    assert "cc_pulldown_ohm" not in slots_flagged(
        gate_findings(parts, netlist=netlist)
    ), "4.7 k is inside the 5.1 k +/-20% band"

    parts, netlist = build("10k")
    hit = next(
        (f for f in gate_findings(parts, netlist=netlist)
         if f.slot == "cc_pulldown_ohm"),
        None,
    )
    assert hit is not None, "10 k is outside the band and must be flagged"


# --------------------------------------------------------------------------- #
# Severity discipline
# --------------------------------------------------------------------------- #

def test_margin_class_slots_warn_rather_than_block() -> None:
    """Consequence decides severity - a margin slot must not halt a build.

    ``required_cin`` is margin class: an undersized input capacitor degrades
    transient response rather than destroying the part, so it is reported and
    left to a reviewer.
    """
    parts = [
        part("U1", "AP2112K-3.3", "ldo regulator"),
        part("C1", "100pF", "input capacitor"),
    ]
    netlist = _Netlist([_Net("5V", ["U1", "C1"]), _Net("3V3", ["U1"])])
    findings = gate_findings(parts, rails=["5V", "3V3"], netlist=netlist)
    hit = next((f for f in findings if f.slot == "required_cin"), None)
    if hit is not None:
        assert hit.severity is Severity.WARNING, (
            "required_cin is declared margin class and must not block"
        )


def test_every_burn_finding_is_an_error() -> None:
    """No burn-class violation may be downgraded to a warning."""
    parts = [
        part("U1", "AP2112K-3.3", "ldo regulator"),
        part("U2", "ATmega328P", "mcu"),
        part("Y1", "X322516MLB4SI 16MHz", "crystal"),
    ]
    findings = gate_findings(parts, rails=["24V", "3V3"])
    burn_slots = {"vin_range", "abs_max_vin", "supply_range", "freq_vs_supply"}
    for f in findings:
        if f.slot in burn_slots:
            assert f.severity is Severity.ERROR, f"{f.slot} must block"



# --------------------------------------------------------------------------- #
# The invariant the old gate violated silently
# --------------------------------------------------------------------------- #

# Comparable slots with NO observer yet, each with its reason. This dict is the
# honest form of the claim "the fact base is consumed": any slot that declares a
# comparison and is not listed here must have a live path from a design to a
# verdict.
#
# It exists because the failure it guards against is invisible. The gate this
# work replaces declared consumers for 37 slots and actually read three, and no
# test noticed - a fact base nobody reads looks exactly like a working one.
UNOBSERVED_SLOTS: dict[str, str] = {
    "drive_level_uw": (
        "A real gate - the drive the oscillator applies must not exceed what the "
        "crystal tolerates - but no crystal in the roster publishes a drive level "
        "(all three are not_asserted), and computing the applied drive needs the "
        "MCU's drive strength plus the series resistor. Nothing to compare on "
        "either side yet."
    ),
    "freq_mhz": (
        "Gated, but not through an observer: a crystal's frequency must match "
        "another DEVICE's fact (the MCU's required frequency or HSE range), not a "
        "design value, so it is handled in cross_device_verdicts. Locked by "
        "test_crystal_frequency_is_gated_without_a_slot_observer."
    ),
}


def test_every_comparable_slot_has_an_observer_or_a_stated_reason() -> None:
    """Each gateable slot must reach a verdict, or be listed as unobservable.

    ``comparison != NONE`` is a promise that a slot gates something. This test
    holds that promise to an implementation, which is exactly what was missing
    when 34 of 37 slots had no path to a verdict and every test still passed.
    """
    obs = factgate.DesignObservation(
        logic_supply_v=3.3, clock_mhz=12.0, crystal_mhz=12.0, highest_rail_v=5.0,
    )
    # Populate every per-part channel so a slot only reports unobserved when no
    # mapping exists for it at all.
    ref = "PROBE"
    obs.pad_counts[ref] = 8
    obs.upstream_v[ref] = 5.0
    obs.protected_rail_v[ref] = 5.0
    obs.cc_pulldown_ohm[ref] = 5100.0
    obs.input_cap_uf[ref] = 1.0
    obs.output_cap_uf[ref] = 22.0

    sample = {
        DeviceClass.MCU: "RP2040",
        DeviceClass.LDO: "AMS1117-3.3",
        DeviceClass.DCDC: "TPS563201",
        DeviceClass.CRYSTAL: "X322512MSB4SI",
        DeviceClass.TVS: "PESD5V0L1BA",
        DeviceClass.CONNECTOR: "USB-C 16P",
    }

    unobserved: dict[str, str] = {}
    for device_class, slot_names in QUESTIONNAIRE.items():
        sheet = fact_sheet(sample[device_class])
        assert sheet is not None, f"no sample sheet for {device_class}"
        for slot_name in slot_names:
            if SLOT_SPECS[slot_name].comparison is Comparison.NONE:
                continue
            if factgate._observed_for(slot_name, sheet, ref, obs) is None:
                unobserved[slot_name] = device_class.value

    unexplained = set(unobserved) - set(UNOBSERVED_SLOTS)
    assert not unexplained, (
        "these slots declare a comparison but nothing reads a design value for "
        f"them, so they can never gate anything: {sorted(unexplained)}. Add an "
        "observer to factgate._observed_for, change the comparison to NONE, or "
        "record the slot in UNOBSERVED_SLOTS with a reason."
    )
    stale = set(UNOBSERVED_SLOTS) - set(unobserved)
    assert not stale, (
        f"listed as unobservable but an observer now exists: {sorted(stale)} - "
        "remove them from UNOBSERVED_SLOTS"
    )


def test_crystal_frequency_is_gated_without_a_slot_observer() -> None:
    """freq_mhz is EXACT with no observer on purpose - prove it still gates.

    Its counterpart is another device's fact rather than a design value. Without
    this test, deleting the cross-device handler would look like a legitimate
    simplification and would silently drop the check.
    """
    assert SLOT_SPECS["freq_mhz"].comparison is Comparison.EXACT
    parts = [
        part("U1", "ESP32-WROOM-32", "mcu"),
        part("Y1", "X322512MSB4SI 12MHz", "crystal"),
    ]
    assert "freq_mhz" in slots_flagged(gate_findings(parts, rails=["3V3"])), (
        "freq_mhz has no entry in _observed_for, so cross_device_verdicts is the "
        "only thing making it gate - it must stay"
    )


def test_connected_rail_beats_the_worst_case_guess() -> None:
    """With a netlist, a regulator is judged on its real input, not the top rail.

    Without connectivity the gate assumes the highest rail on the board feeds
    every regulator, which is the safe worst case but can over-report: a 3.3 V
    LDO fed from 5 V on a board that also carries 24 V would be flagged against
    24 V. The netlist replaces that assumption with the truth.
    """
    parts = [part("U1", "AP2112K-3.3", "ldo regulator")]

    # No netlist: the 24 V rail is assumed to reach the LDO.
    no_net = gate_findings(parts, rails=["24V", "5V", "3V3"])
    assert "abs_max_vin" in slots_flagged(no_net)

    # With a netlist showing it is fed from 5 V, the finding disappears.
    netlist = _Netlist([_Net("5V", ["U1"]), _Net("3V3", ["U1"])])
    obs = observe(parts, rails=["24V", "5V", "3V3"], netlist=netlist)
    assert obs.upstream_v.get("U1") == 5.0, (
        "the netlist must override the worst-case highest-rail assumption"
    )
    with_net = gate_findings(parts, rails=["24V", "5V", "3V3"], netlist=netlist)
    assert "abs_max_vin" not in slots_flagged(with_net)
    assert "vin_range" not in slots_flagged(with_net)


# --------------------------------------------------------------------------- #
# Pad count: cached per library root set, not per footprint id
# --------------------------------------------------------------------------- #


def test_pad_count_is_recomputed_when_the_library_roots_change(
    tmp_path, monkeypatch
) -> None:
    """A footprint id means nothing without the roots it resolves against.

    Keyed on the id alone, the first answer wins for the life of the process: a
    library that was unreachable when the first caller asked stays ``None`` after
    it becomes reachable. Nothing raises — ``None`` is also how "not installed"
    is reported — so every pad-count comparison downstream just quietly stops
    happening. The sibling indexes in :mod:`ratsnestpro.eda.grounding` are keyed
    on their root set for the same reason.
    """
    pretty = tmp_path / "MyLib.pretty"
    pretty.mkdir()
    (pretty / "Part.kicad_mod").write_text(
        '(footprint "Part" (layer "F.Cu")'
        '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))'
        '(pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu")))',
        encoding="utf-8",
    )
    unreachable = tmp_path / "elsewhere"
    unreachable.mkdir()

    # Asked first while the library is out of reach.
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(unreachable))
    assert factgate._footprint_pad_count("MyLib:Part") is None

    # Same id, library now reachable. A stale cache would still say None.
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(tmp_path))
    assert factgate._footprint_pad_count("MyLib:Part") == 2
