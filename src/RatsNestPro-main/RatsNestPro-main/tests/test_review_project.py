"""Task 10: standalone review of an existing KiCad project."""

from __future__ import annotations

import pytest

from ratsnestpro.cli import main
from ratsnestpro.domain.contracts import GateStatus
from ratsnestpro.eda import SchematicDoc
from ratsnestpro.families import Atmega328Params
from ratsnestpro.orchestration import generate_design, review_project
from ratsnestpro.orchestration.review_project import (
    ReviewProjectError,
    _component_release_gate,
)


def test_review_generated_project(tmp_path) -> None:
    # Generate a board, then review the produced project directory.
    gen = generate_design(
        "ATmega328 16MHz 5V LED", params=Atmega328Params(),
        out_dir=tmp_path / "proj", run_erc=False,
    )
    assert gen.schematic_path.exists()
    pr = review_project(tmp_path / "proj", mode="offline")
    assert pr.schematic_path is not None
    assert "# Design Review" in pr.markdown
    # Review runs without crashing and produces gate rows.
    assert "connectivity" in pr.markdown


def test_review_minimal_hand_built_schematic(tmp_path) -> None:
    doc = SchematicDoc.new()
    doc.add_component("Device:R", "R1", "10k", 50, 50)
    doc.add_component("Device:C", "C1", "100nF", 60, 50)
    doc.add_power_symbol("GND", 55, 55)
    sch = doc.save(tmp_path / "mini.kicad_sch")
    pr = review_project(sch, mode="offline")
    assert pr.schematic_path == sch
    assert isinstance(pr.blocked, bool)
    assert "Gate basis" in pr.markdown


def test_review_project_file_discovers_same_stem_schematic(tmp_path) -> None:
    doc = SchematicDoc.new()
    doc.add_component("Device:R", "R1", "10k", 50, 50)
    sch = doc.save(tmp_path / "paired.kicad_sch")
    project = tmp_path / "paired.kicad_pro"
    project.write_text("{}", encoding="utf-8")

    pr = review_project(project, mode="offline")

    assert pr.schematic_path == sch


def test_review_missing_project_raises(tmp_path) -> None:
    with pytest.raises(ReviewProjectError):
        review_project(tmp_path / "nope", mode="offline")


def test_review_empty_dir_raises(tmp_path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(ReviewProjectError):
        review_project(tmp_path / "empty", mode="offline")


def test_missing_procurement_release_proof_is_advisory(tmp_path) -> None:
    gate = _component_release_gate(tmp_path, None, None)

    assert gate.status == GateStatus.FAILED
    assert not gate.required


def test_review_rejects_net_assigned_board_with_no_copper_tracks(tmp_path) -> None:
    pcb = tmp_path / "unrouted.kicad_pcb"
    pcb.write_text(
        """(kicad_pcb
  (version 20240108)
  (generator \"ratsnest-test\")
  (net 0 \"\")
  (net 1 \"SIG\")
  (footprint \"T:Part\" (layer \"F.Cu\") (at 10 10)
    (property \"Reference\" \"R1\" (at 0 0 0))
    (pad \"1\" smd rect (at 0 0) (size 1 1) (layers \"F.Cu\") (net 1 \"SIG\")))
  (footprint \"T:Part\" (layer \"F.Cu\") (at 20 10)
    (property \"Reference\" \"R2\" (at 0 0 0))
    (pad \"1\" smd rect (at 0 0) (size 1 1) (layers \"F.Cu\") (net 1 \"SIG\")))
  (gr_rect (start 0 0) (end 30 20) (stroke (width 0.05) (type default))
    (fill none) (layer \"Edge.Cuts\"))
)\n""",
        encoding="utf-8",
    )

    result = review_project(pcb, mode="offline")
    manufacturing = next(
        gate for gate in result.result.report.gates if gate.gate == "manufacturing"
    )

    assert manufacturing.status == GateStatus.FAILED
    assert manufacturing.required
    assert result.blocked
    assert any(
        "no copper track" in finding.summary
        for finding in manufacturing.findings
    )


def test_cli_review(tmp_path, capsys) -> None:
    generate_design(
        "ATmega328 8MHz 3.3V", params=Atmega328Params(crystal_mhz=8, ldo_output_v=3.3),
        out_dir=tmp_path / "p", run_erc=False,
    )
    rc = main(["review", str(tmp_path / "p"), "--out", str(tmp_path / "review.md")])
    assert rc == 0
    assert (tmp_path / "review.md").exists()
    assert "# Design Review" in capsys.readouterr().out
