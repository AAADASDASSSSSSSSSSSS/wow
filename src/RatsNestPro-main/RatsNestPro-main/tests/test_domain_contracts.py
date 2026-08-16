"""Task 1: domain contract validation and round-trip tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ratsnestpro.domain import (
    CircuitIR,
    ComponentSpec,
    Finding,
    GateResult,
    GateStatus,
    NetSpec,
    PinRef,
    Severity,
    VerificationReport,
)


def _mini_ir() -> CircuitIR:
    return CircuitIR(
        family="atmega328-dev-board",
        components=[
            ComponentSpec(ref="U1", symbol="MCU:X", value="X", role="mcu"),
            ComponentSpec(ref="R1", symbol="Device:R", value="10k", role="pullup"),
        ],
        nets=[
            NetSpec(name="VCC", pins=[PinRef(component_ref="U1", pin="1")]),
            NetSpec(
                name="RESET",
                pins=[PinRef(component_ref="U1", pin="2"), PinRef(component_ref="R1", pin="1")],
            ),
        ],
    )


def test_valid_ir_round_trips_through_json() -> None:
    ir = _mini_ir()
    dumped = ir.model_dump_json()
    restored = CircuitIR.model_validate_json(dumped)
    assert restored == ir
    assert restored.component("U1") is not None
    assert restored.net("RESET") is not None
    assert [c.ref for c in restored.components_with_role("mcu")] == ["U1"]


def test_duplicate_component_refs_rejected() -> None:
    with pytest.raises(ValidationError):
        CircuitIR(
            components=[
                ComponentSpec(ref="R1", symbol="Device:R", value="1k"),
                ComponentSpec(ref="R1", symbol="Device:R", value="2k"),
            ]
        )


def test_net_referencing_unknown_component_rejected() -> None:
    with pytest.raises(ValidationError):
        CircuitIR(
            components=[ComponentSpec(ref="U1", symbol="MCU:X", value="X")],
            nets=[NetSpec(name="N", pins=[PinRef(component_ref="U9", pin="1")])],
        )


def test_pin_assigned_to_two_nets_rejected() -> None:
    with pytest.raises(ValidationError):
        CircuitIR(
            components=[ComponentSpec(ref="U1", symbol="MCU:X", value="X")],
            nets=[
                NetSpec(name="A", pins=[PinRef(component_ref="U1", pin="1")]),
                NetSpec(name="B", pins=[PinRef(component_ref="U1", pin="1")]),
            ],
        )


def test_duplicate_pins_within_net_rejected() -> None:
    with pytest.raises(ValidationError):
        NetSpec(
            name="N",
            pins=[PinRef(component_ref="U1", pin="1"), PinRef(component_ref="U1", pin="1")],
        )


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        ComponentSpec(ref="R1", symbol="Device:R", value="1k", bogus="x")  # type: ignore[call-arg]


def test_verification_report_blocked_logic() -> None:
    ok = VerificationReport(
        gates=[GateResult(gate="g1", status=GateStatus.PASSED, required=True)]
    )
    assert ok.blocked is False

    failed = VerificationReport(
        gates=[GateResult(gate="g1", status=GateStatus.FAILED, required=True)]
    )
    assert failed.blocked is True

    error_finding = VerificationReport(
        gates=[
            GateResult(
                gate="g1",
                status=GateStatus.PASSED,
                required=True,
                findings=[
                    Finding(severity=Severity.ERROR, rule_id="X-001", summary="boom")
                ],
            )
        ]
    )
    assert error_finding.blocked is True
    assert len(error_finding.findings) == 1


def test_unavailable_required_gate_is_not_a_pass() -> None:
    report = VerificationReport(
        gates=[GateResult(gate="kicad_erc", status=GateStatus.UNAVAILABLE, required=True)]
    )
    # Unavailable is not blocked (tool missing) but also not passed — callers
    # must not treat it as a pass.
    assert report.gate("kicad_erc") is not None
    assert report.gate("kicad_erc").passed is False
