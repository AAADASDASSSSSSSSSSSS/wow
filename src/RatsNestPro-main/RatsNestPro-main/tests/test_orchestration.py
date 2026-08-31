"""Task 5: deterministic generation loop + CLI."""

from __future__ import annotations

import json
import os

import pytest

from ratsnestpro.cli import main, params_from_requirement
from ratsnestpro.domain.contracts import GateStatus
from ratsnestpro.eda import SchematicDoc
from ratsnestpro.families import Atmega328Params
from ratsnestpro.orchestration import generate_design

REAL_KICAD = os.environ.get("RATSNESTPRO_RUN_REAL_KICAD_TESTS") == "1"


def test_generate_writes_run_dir_and_passes(tmp_path) -> None:
    result = generate_design(
        "ATmega328 dev board, USB-C, 16MHz, 5V, power LED",
        params=Atmega328Params(),
        out_dir=tmp_path / "run",
        run_erc=False,  # deterministic-only
    )
    assert result.plan_path.exists()
    assert result.schematic_path.exists()
    assert result.report_path.exists()
    assert result.blocked is False

    # plan.json round-trips
    plan = json.loads(result.plan_path.read_text(encoding="utf-8"))
    assert plan["circuit"]["family"] == "atmega328-dev-board"

    # The materialized schematic reloads and its label-netlist matches the IR nets.
    doc = SchematicDoc.load(result.schematic_path)
    label_nets = set(doc.label_netlist().keys())
    ir_nets = {n["name"] for n in plan["circuit"]["nets"]}
    assert ir_nets.issubset(label_nets)
    # Every component reference is present in the schematic.
    assert {c["ref"] for c in plan["circuit"]["components"]}.issubset(set(doc.references()))


def test_two_requirements_produce_different_boards(tmp_path) -> None:
    a = generate_design(
        "16MHz with LED", params=Atmega328Params(crystal_mhz=16, ldo_output_v=5.0, power_led=True),
        out_dir=tmp_path / "a", run_erc=False,
    )
    b = generate_design(
        "8MHz no LED", params=Atmega328Params(crystal_mhz=8, ldo_output_v=3.3, power_led=False),
        out_dir=tmp_path / "b", run_erc=False,
    )
    da = SchematicDoc.load(a.schematic_path)
    db = SchematicDoc.load(b.schematic_path)
    assert "D1" in da.references()
    assert "D1" not in db.references()


def test_keyword_extractor() -> None:
    assert params_from_requirement("use an 8MHz crystal, no LED") == {
        "crystal_mhz": 8,
        "power_led": False,
    }
    got = params_from_requirement("16MHz 5V with power LED")
    assert got["crystal_mhz"] == 16 and got["ldo_output_v"] == 5.0 and got["power_led"] is True


def test_cli_design_plan(tmp_path, capsys) -> None:
    rc = main(["design-plan", "ATmega328 8MHz 3.3V no LED", "--out", str(tmp_path / "p")])
    assert rc == 0
    assert (tmp_path / "p" / "plan.json").exists()


def test_cli_design_deterministic(tmp_path, capsys) -> None:
    rc = main(["design", "ATmega328 16MHz 5V LED", "--out", str(tmp_path / "d"), "--no-erc"])
    assert rc == 0
    assert (tmp_path / "d" / "atmega328_dev_board.kicad_sch").exists()


def test_cli_rejects_contradictory_params(tmp_path, capsys) -> None:
    # 16 MHz on 3.3 V is contradictory → the Architect asks to clarify (exit 3).
    rc = main(["design", "ATmega328 16MHz 3.3V", "--out", str(tmp_path / "x"), "--no-erc"])
    assert rc in (2, 3)
    err = capsys.readouterr().err.lower()
    assert "clarify" in err or "5.0 v" in err or "supply" in err


@pytest.mark.real_kicad
@pytest.mark.skipif(not REAL_KICAD, reason="requires real KiCad 10 CLI (opt-in)")
def test_generate_runs_real_erc(tmp_path) -> None:
    # With real KiCad, the complete reference must pass, not merely execute ERC.
    result = generate_design(
        "ATmega328 16MHz 5V LED", params=Atmega328Params(),
        out_dir=tmp_path / "real", run_erc=True,
    )
    erc = result.report.gate("kicad_erc")
    assert erc is not None and erc.status == GateStatus.PASSED
    assert not [finding for finding in erc.findings if finding.severity.value == "error"]
