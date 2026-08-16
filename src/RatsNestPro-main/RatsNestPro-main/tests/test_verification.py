"""Task 3: deterministic verification gates + parametric rules."""

from __future__ import annotations

from ratsnestpro.domain.contracts import GateStatus, Severity
from ratsnestpro.families import Atmega328Params, build_ir, expectations_for
from ratsnestpro.verification import verify_design


def _deterministic_gates_pass(report) -> bool:
    return all(
        g.status == GateStatus.PASSED
        for g in report.gates
        if g.gate != "kicad_erc"
    )


def test_reference_design_passes_all_deterministic_gates() -> None:
    p = Atmega328Params()
    ir = build_ir(p)
    report = verify_design(ir, expectations_for(p))  # no sch_path → ERC unavailable
    assert _deterministic_gates_pass(report)
    # ERC is UNAVAILABLE (not run), which is not a block and not a pass.
    erc = report.gate("kicad_erc")
    assert erc.status == GateStatus.UNAVAILABLE
    assert report.blocked is False


def test_alternate_valid_params_also_pass() -> None:
    p = Atmega328Params(
        crystal_mhz=8, ldo_output_v=3.3, decoupling_count=4, power_led=False,
        breakout_rows=1, breakout_pins_per_row=6, mounting_holes=0,
    )
    ir = build_ir(p)
    report = verify_design(ir, expectations_for(p))
    assert _deterministic_gates_pass(report)


def test_wrong_decoupling_count_blocks() -> None:
    # Build with 4 caps but expect 6 → six_decoupling gate fails.
    ir = build_ir(Atmega328Params(crystal_mhz=8, ldo_output_v=3.3, decoupling_count=4))
    exp = expectations_for(Atmega328Params(crystal_mhz=8, ldo_output_v=3.3, decoupling_count=6))
    report = verify_design(ir, exp)
    gate = report.gate("six_decoupling")
    assert gate.status == GateStatus.FAILED
    assert any(f.rule_id == "DEC-001" for f in gate.findings)
    assert report.blocked is True


def test_wrong_crystal_load_cap_blocks() -> None:
    # Build an 8 MHz board (22pF) but expect a 16 MHz board (18pF).
    ir = build_ir(Atmega328Params(crystal_mhz=8, ldo_output_v=3.3))
    exp = expectations_for(Atmega328Params(crystal_mhz=16, ldo_output_v=5.0))
    report = verify_design(ir, exp)
    # crystal_load fails (value mismatch) and voltage fails (3.3 vs 5.0).
    assert report.gate("crystal_load").status == GateStatus.FAILED
    assert report.gate("voltage").status == GateStatus.FAILED
    assert report.blocked is True


def test_missing_supply_net_flagged() -> None:
    p = Atmega328Params()
    ir = build_ir(p)
    # Rename the supply net to simulate a broken IR.
    for net in ir.nets:
        if net.name == "3V3":
            net.name = "VDD_BROKEN"
            break
    report = verify_design(ir, expectations_for(p))
    assert report.gate("voltage").status == GateStatus.FAILED


def test_findings_carry_rule_ids_and_severity() -> None:
    ir = build_ir(Atmega328Params(decoupling_count=4))
    exp = expectations_for(Atmega328Params(decoupling_count=6))
    report = verify_design(ir, exp)
    findings = report.findings
    assert findings, "expected at least one finding"
    assert all(f.rule_id for f in findings)
    assert any(f.severity == Severity.ERROR for f in findings)
