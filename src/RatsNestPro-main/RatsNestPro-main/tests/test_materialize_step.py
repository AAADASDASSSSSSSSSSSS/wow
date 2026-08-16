"""Task 9: materialize .kicad_sch with real symbol geometry + round-trip check."""

from __future__ import annotations

import pytest

from ratsnestpro import config
from ratsnestpro.agents.llm import LlmMode
from ratsnestpro.eda import SchematicDoc, symbols
from ratsnestpro.eda.materialize import materialize_pinmapped
from ratsnestpro.orchestration.pipeline import (
    PipelineContext,
    PipelineState,
    PipelineStep,
    SchMaterializeStep,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    MappedNet,
    MappedPin,
    MaterializeResult,
    NetIntent,
    NetlistIntent,
    PinMapPlan,
    SchLayoutPlan,
    SelectedPart,
    SelectionPlan,
    SheetPlacement,
)


@pytest.fixture(autouse=True)
def _libs(tmp_path, monkeypatch):
    symbols._load_lib_node.cache_clear()
    monkeypatch.setattr(config, "_ENV_LOADED", True)
    symdir = tmp_path / "T.kicad_symdir"
    symdir.mkdir()

    def lib(name, pins):
        return (f'(kicad_symbol_lib (version 20231120) (generator "t")'
                f'(symbol "{name}" (symbol "{name}_1_1" {pins})))')

    def pin(t, n, num, x, y):
        return f'(pin {t} line (at {x} {y} 0) (length 2.54) (name "{n}") (number "{num}"))'

    (symdir / "R.kicad_sym").write_text(
        lib("R", pin("passive", "~", "1", 0, 3.81) + pin("passive", "~", "2", 0, -3.81)),
        encoding="utf-8",
    )
    (symdir / "C.kicad_sym").write_text(
        lib("C", pin("passive", "~", "1", 0, 2.54) + pin("passive", "~", "2", 0, -2.54)),
        encoding="utf-8",
    )
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(tmp_path))
    yield
    symbols._load_lib_node.cache_clear()


def _state() -> PipelineState:
    s = PipelineState(requirement_text="x", project_name="demo")
    s.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(ref="R1", symbol="T:R", value="10k", footprint=""),
        SelectedPart(ref="C1", symbol="T:C", value="1uF", footprint=""),
    ])
    s.artifacts[PipelineStep.SCH_CONNECTIONS] = NetlistIntent(
        nets=[NetIntent(name="N1", kind="signal", pins=[]),
              NetIntent(name="GND", kind="ground", pins=[])],
        supply_nets=["N1"], ground_net="GND",
    )
    s.artifacts[PipelineStep.SCH_LAYOUT] = SchLayoutPlan(placements=[
        SheetPlacement(ref="R1", x=50.0, y=50.0, rotation=0.0),
        SheetPlacement(ref="C1", x=80.0, y=50.0, rotation=0.0),
    ])
    s.artifacts[PipelineStep.SCH_PINMAP] = PinMapPlan(nets=[
        MappedNet(name="N1", kind="signal", pins=[
            MappedPin(ref="R1", logical="1", number="1"),
            MappedPin(ref="C1", logical="1", number="1")]),
        MappedNet(name="GND", kind="ground", pins=[
            MappedPin(ref="R1", logical="2", number="2"),
            MappedPin(ref="C1", logical="2", number="2")]),
    ])
    return s


def test_materialize_writes_sch_and_round_trips(tmp_path) -> None:
    state = _state()
    SchMaterializeStep().run(state, PipelineContext(mode=LlmMode.OFFLINE, out_dir=str(tmp_path)))
    art = state.artifact(PipelineStep.SCH_MATERIALIZE)
    assert isinstance(art, MaterializeResult)
    assert (tmp_path / "demo.kicad_sch").exists()
    result = state.results[-1]
    assert not result.blocked
    assert all(c.ok for c in result.checks)
    # Reload and confirm the label netlist matches the pin map.
    doc = SchematicDoc.load(art.sch_path)
    netlist = doc.label_netlist()
    assert set(netlist) >= {"N1", "GND"}
    assert len(netlist["N1"]) == 2 and len(netlist["GND"]) == 2
    assert {"R1", "C1"} <= set(doc.references())


def test_labels_sit_on_real_pin_geometry(tmp_path) -> None:
    # R1 at (50,50); pin 1 local (0, 3.81) Y-up -> schematic (50, 50-3.81=46.19).
    state = _state()
    SchMaterializeStep().run(state, PipelineContext(mode=LlmMode.OFFLINE, out_dir=str(tmp_path)))
    doc = SchematicDoc.load(state.artifact(PipelineStep.SCH_MATERIALIZE).sch_path)
    coords = {(round(x, 2), round(y, 2)) for x, y in doc.label_netlist()["N1"]}
    assert (50.0, 46.19) in coords  # R1 pin 1 at real transformed geometry


def test_materialize_pinmapped_helper_direct() -> None:
    comps = [
        {"ref": "R1", "symbol": "T:R", "value": "10k", "footprint": "", "x": 0, "y": 0,
         "rotation": 0},
    ]
    nets = [{"name": "A", "pins": [{"ref": "R1", "number": "1"}]}]
    doc = materialize_pinmapped(comps, nets, supply_nets=[], ground_net="GND")
    assert "R1" in doc.references()
    assert "A" in doc.label_netlist()


def test_materialize_does_not_add_power_flag_to_driven_output(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        symbols,
        "symbol_pins",
        lambda _lib_id: [
            {
                "number": "1",
                "name": "OUT",
                "type": "power_out",
                "x": 0.0,
                "y": 0.0,
            },
            {
                "number": "2",
                "name": "IN",
                "type": "power_in",
                "x": 2.54,
                "y": 0.0,
            },
        ],
    )
    doc = materialize_pinmapped(
        [
            {
                "ref": "U1",
                "symbol": "Test:Source",
                "value": "Source",
                "footprint": "",
                "x": 20,
                "y": 20,
                "rotation": 0,
            }
        ],
        [
            {"name": "3V3", "pins": [{"ref": "U1", "number": "1"}]},
            {"name": "VIN", "pins": [{"ref": "U1", "number": "2"}]},
        ],
        supply_nets=["3V3", "VIN"],
    )

    text = doc.raw.to_text()
    assert text.count('(lib_id "power:PWR_FLAG")') == 1


def test_materialize_writes_explicit_no_connect_marker() -> None:
    comps = [
        {
            "ref": "R1",
            "symbol": "T:R",
            "value": "10k",
            "footprint": "",
            "x": 50,
            "y": 50,
            "rotation": 0,
        },
    ]
    nets = [{"name": "A", "pins": [{"ref": "R1", "number": "1"}]}]

    doc = materialize_pinmapped(
        comps,
        nets,
        no_connect_pins=[{"ref": "R1", "number": "2"}],
    )

    assert "(no_connect" in doc.raw.to_text()
