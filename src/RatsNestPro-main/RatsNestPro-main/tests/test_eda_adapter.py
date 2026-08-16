"""Task 2: EDA adapter — schematic materialization, connectivity, ERC."""

from __future__ import annotations

import os

import pytest

from ratsnestpro.eda import SchematicDoc, kicad_cli_available, run_erc

REAL_KICAD = os.environ.get("RATSNESTPRO_RUN_REAL_KICAD_TESTS") == "1"


def test_minimal_schematic_saves_and_reloads(tmp_path) -> None:
    doc = SchematicDoc.new()
    doc.add_component("Device:R", "R1", "10k", 50, 50)
    doc.add_component("Device:R", "R2", "20k", 70, 50)
    doc.add_wire(52.54, 50, 67.46, 50)
    path = doc.save(tmp_path / "mini.kicad_sch")
    assert path.exists()

    reloaded = SchematicDoc.load(path)
    assert set(reloaded.references()) == {"R1", "R2"}


def test_wire_unions_endpoints_in_topology(tmp_path) -> None:
    doc = SchematicDoc.new()
    doc.add_component("Device:R", "R1", "10k", 50, 50)
    doc.add_component("Device:R", "R2", "20k", 70, 50)
    # One wire joining two points → those points share one topology component.
    doc.add_wire(52.54, 50, 67.46, 50)
    doc.save(tmp_path / "w.kicad_sch")

    comps = doc.topology_components()
    joined = [c for c in comps if [52.54, 50.0] in c["points"] and [67.46, 50.0] in c["points"]]
    assert len(joined) == 1


def test_label_netlist_groups_by_name(tmp_path) -> None:
    doc = SchematicDoc.new()
    doc.add_component("Device:R", "R1", "10k", 50, 50)
    doc.add_component("Device:C", "C1", "100nF", 70, 50)
    doc.add_net_label("VCC", 50, 47.46)
    doc.add_net_label("VCC", 70, 47.46)
    doc.add_net_label("GND", 50, 52.54)
    doc.save(tmp_path / "labels.kicad_sch")

    netlist = doc.label_netlist()
    assert set(netlist.keys()) == {"VCC", "GND"}
    assert len(netlist["VCC"]) == 2
    assert "VCC" in doc.nets() and "GND" in doc.nets()


def test_power_symbol_contributes_net(tmp_path) -> None:
    doc = SchematicDoc.new()
    doc.add_component("Device:R", "R1", "10k", 50, 50)
    doc.add_power_symbol("GND", 50, 55)
    doc.save(tmp_path / "pwr.kicad_sch")
    assert "GND" in doc.nets()


def test_erc_reports_unavailable_when_no_cli(tmp_path) -> None:
    doc = SchematicDoc.new()
    doc.add_component("Device:R", "R1", "10k", 50, 50)
    sch = doc.save(tmp_path / "erc.kicad_sch")
    result = run_erc(sch, out_dir=tmp_path)
    # Missing kicad-cli must be reported as unavailable, never as a pass.
    if result.available:
        assert result.ran in (True, False)
    else:
        assert result.ok is False and result.ran is False


@pytest.mark.real_kicad
@pytest.mark.skipif(not REAL_KICAD, reason="requires real KiCad 10 CLI (opt-in)")
def test_erc_runs_with_real_kicad(tmp_path) -> None:
    assert kicad_cli_available() is not None
    doc = SchematicDoc.new()
    doc.add_component("Device:R", "R1", "10k", 50, 50)
    doc.add_power_symbol("GND", 50, 55)
    sch = doc.save(tmp_path / "real.kicad_sch")
    result = run_erc(sch, out_dir=tmp_path)
    assert result.available and result.ran
