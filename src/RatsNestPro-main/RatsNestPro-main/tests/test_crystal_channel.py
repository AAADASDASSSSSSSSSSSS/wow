"""A crystal must sit on the oscillator channel rated for its frequency.

The defect: an 8 MHz crystal wired to the STM32F103's pins 3 and 4. Those are
``PC14`` / ``PC15``, whose alternates are ``RCC_OSC32_IN`` / ``RCC_OSC32_OUT`` —
the 32.768 kHz low-speed channel. Pins 5 and 6 (``RCC_OSC_IN`` / ``RCC_OSC_OUT``)
were never connected, and the nets were named ``HSE_OSC_IN`` / ``HSE_OSC_OUT``.
The intent was right and the pins were wrong, which is why nothing keyed on names
could see it, and why ``crystal_two_distinct_signal_nets`` passes: the terminals
do land on two distinct non-power nets.

The channel is stated in exactly one place — the symbol library's alternate
function names — so unlike the other checks added alongside it, this one has to
read the library. That is why the tests here stub it rather than avoid it.
"""

from __future__ import annotations

import pytest

from ratsnestpro.domain.contracts import Severity
from ratsnestpro.eda import factgate
from ratsnestpro.orchestration.pipeline_contracts import SelectedPart
from tests.fixtures import kicad_demos as demos

# The four pins that matter, as MCU_ST_STM32F1 declares them via its base symbol.
_LIB_PINS = [
    {"number": "3", "name": "PC14", "type": "bidirectional",
     "alternates": ("RCC_OSC32_IN",)},
    {"number": "4", "name": "PC15", "type": "bidirectional",
     "alternates": ("ADC1_EXTI15", "ADC2_EXTI15", "RCC_OSC32_OUT")},
    {"number": "5", "name": "PD0", "type": "bidirectional",
     "alternates": ("RCC_OSC_IN",)},
    {"number": "6", "name": "PD1", "type": "bidirectional",
     "alternates": ("RCC_OSC_OUT",)},
    {"number": "24", "name": "VDD", "type": "power_in", "alternates": ()},
]


def _mcu() -> SelectedPart:
    return SelectedPart(
        ref="U1",
        symbol="MCU_ST_STM32F1:STM32F103C8Tx",
        value="STM32F103C8Tx",
        role="",
    )


def _crystal(value: str = "8MHz") -> SelectedPart:
    return SelectedPart(ref="X1", symbol="Device:Crystal", value=value, role="")


@pytest.fixture
def stub_library(monkeypatch):
    """Serve ``_LIB_PINS`` for the MCU and two bare pins for anything else."""
    from ratsnestpro.eda import symbols

    def _pins(lib_id: str):
        if "STM32" in lib_id:
            return list(_LIB_PINS)
        return [
            {"number": "1", "name": "", "type": "passive", "alternates": ()},
            {"number": "2", "name": "", "type": "passive", "alternates": ()},
        ]

    monkeypatch.setattr(symbols, "symbol_pins", _pins)


def _findings(parts, pin_nets):
    return factgate.crystal_channel_conflicts(parts, pin_nets=pin_nets)


# --------------------------------------------------------------------------- #
# Positive
# --------------------------------------------------------------------------- #


def test_megahertz_crystal_on_the_low_speed_channel_is_an_error(stub_library) -> None:
    findings = _findings(
        [_mcu(), _crystal()],
        {
            ("U1", "3"): "/HSE_OSC_IN",
            ("U1", "4"): "/HSE_OSC_OUT",
            ("X1", "1"): "/HSE_OSC_IN",
            ("X1", "2"): "/HSE_OSC_OUT",
        },
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.ref == "X1"
    assert finding.slot == "clock_external"
    assert finding.severity is Severity.ERROR
    assert "8 MHz" in finding.message
    assert "32.768 kHz low-speed" in finding.message
    # Both the wrong pins and the right ones are named, so the repair is actionable.
    assert "U1:3" in finding.message
    assert "U1:5" in finding.message
    # The citation is the page that states the range, not the sheet header.
    assert "4-16" in finding.message or "4 to 16" in finding.citation
    assert set(finding.all_targets()) >= {"X1", "U1"}


def test_net_names_do_not_launder_the_wrong_pins(stub_library) -> None:
    """Naming a net HSE_OSC_IN does not make pin 3 the HSE channel."""
    findings = _findings(
        [_mcu(), _crystal()],
        {
            ("U1", "3"): "/HSE_OSC_IN",
            ("U1", "4"): "/HSE_OSC_OUT",
            ("X1", "1"): "/HSE_OSC_IN",
            ("X1", "2"): "/HSE_OSC_OUT",
        },
    )
    assert len(findings) == 1
    assert "does not move them" in findings[0].message


# --------------------------------------------------------------------------- #
# Negative
# --------------------------------------------------------------------------- #


def test_crystal_on_the_high_speed_channel_is_accepted(stub_library) -> None:
    assert not _findings(
        [_mcu(), _crystal()],
        {
            ("U1", "5"): "/HSE_IN",
            ("U1", "6"): "/HSE_OUT",
            ("X1", "1"): "/HSE_IN",
            ("X1", "2"): "/HSE_OUT",
        },
    )


def test_low_speed_crystal_on_the_low_speed_channel_is_accepted(stub_library) -> None:
    """A 32.768 kHz value carries no megahertz figure, so no verdict is reached.

    That is the correct outcome and the reason the frequency test is in
    megahertz: the low-speed channel is exactly where such a crystal belongs.
    """
    assert not _findings(
        [_mcu(), _crystal("32.768kHz")],
        {
            ("U1", "3"): "/LSE_IN",
            ("U1", "4"): "/LSE_OUT",
            ("X1", "1"): "/LSE_IN",
            ("X1", "2"): "/LSE_OUT",
        },
    )


def test_symbol_without_alternates_yields_no_verdict(monkeypatch) -> None:
    """Older libraries declare none; silence is the only honest answer."""
    from ratsnestpro.eda import symbols

    monkeypatch.setattr(
        symbols,
        "symbol_pins",
        lambda _lib_id: [
            {"number": n, "name": f"P{n}", "type": "bidirectional", "alternates": ()}
            for n in ("3", "4", "5", "6")
        ],
    )
    assert not _findings(
        [_mcu(), _crystal()],
        {
            ("U1", "3"): "/A",
            ("U1", "4"): "/B",
            ("X1", "1"): "/A",
            ("X1", "2"): "/B",
        },
    )


def test_unknown_mcu_yields_no_verdict(stub_library) -> None:
    """No fact sheet means no rated range to compare against."""
    unknown = SelectedPart(
        ref="U1", symbol="MCU_Microchip_PIC16:PIC16F54", value="PIC16F54", role=""
    )
    assert not _findings(
        [unknown, _crystal()],
        {
            ("U1", "3"): "/A",
            ("U1", "4"): "/B",
            ("X1", "1"): "/A",
            ("X1", "2"): "/B",
        },
    )


def test_crystal_without_a_parseable_frequency_yields_no_verdict(stub_library) -> None:
    assert not _findings(
        [_mcu(), _crystal("Crystal")],
        {
            ("U1", "3"): "/A",
            ("U1", "4"): "/B",
            ("X1", "1"): "/A",
            ("X1", "2"): "/B",
        },
    )


def test_a_part_on_only_one_oscillator_pin_is_not_a_crystal(stub_library) -> None:
    """A load capacitor touches one oscillator net and ground, not both nets."""
    cap = SelectedPart(ref="C4", symbol="Device:C", value="20pF", role="")
    assert not _findings(
        [_mcu(), cap],
        {
            ("U1", "3"): "/A",
            ("U1", "4"): "/B",
            ("C4", "1"): "/A",
            ("C4", "2"): "/GND",
        },
    )


def test_alternate_match_is_exact_not_substring(stub_library) -> None:
    """``RCC_OSC32_IN`` contains ``OSC_IN``.

    A substring test would classify the low-speed pins as high-speed and the
    check would accept the very defect it exists for.
    """
    from ratsnestpro.eda.factgate import _channel_pins

    hse = _channel_pins(
        _mcu(), {("U1", "3"): "/A", ("U1", "5"): "/B"}, ("RCC_OSC_IN",)
    )
    assert hse == {"5": "/B"}


def test_repair_hint_names_the_right_pins_even_when_they_are_unwired(
    stub_library,
) -> None:
    """The hint is needed exactly when the correct channel is unconnected.

    Which pins are the high-speed channel is a fact about the symbol, so it must
    not be filtered by connectivity — the first version of this check did filter,
    and lost the hint on the one board shape it was written for.
    """
    findings = _findings(
        [_mcu(), _crystal()],
        {
            ("U1", "3"): "/HSE_OSC_IN",
            ("U1", "4"): "/HSE_OSC_OUT",
            ("X1", "1"): "/HSE_OSC_IN",
            ("X1", "2"): "/HSE_OSC_OUT",
        },
    )
    assert len(findings) == 1
    assert "The high-speed channel is U1:5, U1:6" in findings[0].message


# --------------------------------------------------------------------------- #
# Against the real library and the run that shipped the defect
# --------------------------------------------------------------------------- #


@demos.requires_positive_sample
@demos.requires_kicad_cli
def test_shipped_run_reproduces_the_defect() -> None:
    """No stub: real symbol library, real netlist, real fact sheet."""
    from ratsnestpro.orchestration.pipeline import (
        _ConnectivityView,
        _crystal_channel_checks,
    )

    run = demos.positive_sample_run()
    assert run is not None
    view = _ConnectivityView.from_schematic(run / "stm32f103c8t6-board.kicad_sch")
    # The evidence, stated independently of the check.
    assert view.pin_nets[("X1", "1")] == "/HSE_OSC_IN"
    assert view.pin_nets[("U1", "3")] == "/HSE_OSC_IN"
    # The real HSE channel. KiCad gives an unconnected pin a net of its own
    # rather than omitting it, so "not wired" reads as an ``unconnected-`` name.
    assert view.pin_nets[("U1", "5")].startswith("unconnected-")
    assert view.pin_nets[("U1", "6")].startswith("unconnected-")

    checks = _crystal_channel_checks(view)
    assert [c.name for c in checks] == ["crystal_on_rated_oscillator_channel:X1"]
    assert checks[0].severity is Severity.ERROR


@demos.requires_demos
def test_real_library_declares_the_channels() -> None:
    """The data this check depends on has to survive a KiCad upgrade.

    ``STM32F103C8Tx`` has no pins of its own — it extends
    ``STM32F103C_8-B_Tx`` — so this also guards that resolution.
    """
    from ratsnestpro.eda import symbols

    pins = symbols.symbol_pins("MCU_ST_STM32F1:STM32F103C8Tx")
    if not pins:
        pytest.skip("MCU_ST_STM32F1 not in the installed symbol library")
    alternates = {
        str(p["number"]): tuple(p.get("alternates") or ()) for p in pins
    }
    assert "RCC_OSC32_IN" in alternates["3"]
    assert "RCC_OSC32_OUT" in alternates["4"]
    assert "RCC_OSC_IN" in alternates["5"]
    assert "RCC_OSC_OUT" in alternates["6"]


@pytest.mark.real_kicad
def test_demo_corpus_reports_nothing() -> None:
    """False-positive floor. Weak evidence — one demo project has an MCU sheet."""
    from ratsnestpro.orchestration.pipeline import (
        _ConnectivityView,
        _crystal_channel_checks,
    )

    for path, _netlist in demos.demo_netlists():
        view = _ConnectivityView.from_schematic(path)
        assert not _crystal_channel_checks(view), f"fired on {path.parent.name}"
