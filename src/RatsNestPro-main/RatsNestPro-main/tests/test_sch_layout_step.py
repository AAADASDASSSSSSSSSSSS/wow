"""Task 8: schematic sheet layout + wire/label expression + bottom-line check."""

from __future__ import annotations

import json

import ratsnestpro.orchestration.pipeline as pipeline
from ratsnestpro.agents.llm import LlmMode
from ratsnestpro.orchestration.pipeline import (
    PipelineContext,
    PipelineState,
    PipelineStep,
    SchLayoutStep,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    LogicalPin,
    NetIntent,
    NetlistIntent,
    SchLayoutPlan,
    SelectedPart,
    SelectionPlan,
)


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def complete(self, system, user, temperature=0.2):
        return self._responses.pop(0) if self._responses else "{}"


def _state() -> PipelineState:
    s = PipelineState(requirement_text="x")
    s.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(ref="U1", symbol="T:LDO", value="ldo"),
        SelectedPart(ref="C1", symbol="T:C", value="1uF"),
        SelectedPart(ref="R1", symbol="T:R", value="10k"),
    ])
    s.artifacts[PipelineStep.SCH_CONNECTIONS] = NetlistIntent(
        nets=[
            NetIntent(name="3V3", kind="power",
                      pins=[LogicalPin(ref="U1", pin="OUT"), LogicalPin(ref="C1", pin="1")]),
            NetIntent(name="GND", kind="ground",
                      pins=[LogicalPin(ref="U1", pin="GND"), LogicalPin(ref="C1", pin="2")]),
        ],
        supply_nets=["3V3"], ground_net="GND",
    )
    return s


def test_offline_grid_layout_no_overlap() -> None:
    state = _state()
    SchLayoutStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    plan = state.artifact(PipelineStep.SCH_LAYOUT)
    assert isinstance(plan, SchLayoutPlan)
    assert {p.ref for p in plan.placements} == {"U1", "C1", "R1"}
    # Power + ground drawn as labels.
    assert set(plan.label_nets) == {"3V3", "GND"}
    result = state.results[-1]
    assert not result.blocked
    assert all(c.ok for c in result.checks)


def test_overlapping_symbols_reflowed() -> None:
    # Schematic symbol XY is cosmetic (connectivity is by net label). When the
    # LLM stacks symbols on the same coordinate, the layout step re-flows them
    # onto a clean grid (like KiCad's auto-arrange) rather than failing closed.
    plan = json.dumps({
        "placements": [
            {"ref": "U1", "x": 10, "y": 10, "rotation": 0},
            {"ref": "C1", "x": 10, "y": 10, "rotation": 0},
            {"ref": "R1", "x": 10, "y": 10, "rotation": 0},
        ],
        "label_nets": ["3V3", "GND"],
    })
    state = _state()
    SchLayoutStep().run(state, PipelineContext(mode=LlmMode.AUTO, client=FakeLLM([plan])))
    plan_out = state.artifact(PipelineStep.SCH_LAYOUT)
    assert isinstance(plan_out, SchLayoutPlan)
    # Every part is still placed, the LLM's label choice is preserved, and the
    # re-flow produced a non-overlapping layout, so nothing blocks.
    assert {p.ref for p in plan_out.placements} == {"U1", "C1", "R1"}
    coords = {(p.x, p.y) for p in plan_out.placements}
    assert len(coords) == 3  # no two symbols share a coordinate any more
    result = state.results[-1]
    assert not result.blocked
    assert any(c.name == "no_symbol_overlap" and c.ok for c in result.checks)


def test_real_pin_extents_trigger_reflow(monkeypatch) -> None:
    def fake_symbol_pins(symbol):
        if symbol == "T:LDO":
            return [
                {"number": "1", "x": 0.0, "y": -50.0},
                {"number": "2", "x": 0.0, "y": 50.0},
            ]
        return [
            {"number": "1", "x": 0.0, "y": -2.54},
            {"number": "2", "x": 0.0, "y": 2.54},
        ]

    monkeypatch.setattr(pipeline.symbols, "symbol_pins", fake_symbol_pins)
    plan = json.dumps({
        "placements": [
            {"ref": "U1", "x": 0, "y": 0},
            {"ref": "C1", "x": 0, "y": 25.4},
            {"ref": "R1", "x": 25.4, "y": 0},
        ],
        "label_nets": ["3V3", "GND"],
    })
    state = _state()
    SchLayoutStep().run(
        state, PipelineContext(mode=LlmMode.AUTO, client=FakeLLM([plan]))
    )
    plan_out = state.artifact(PipelineStep.SCH_LAYOUT)
    assert isinstance(plan_out, SchLayoutPlan)
    symbols_by_ref = {"U1": "T:LDO", "C1": "T:C", "R1": "T:R"}
    assert pipeline._sheet_overlaps(plan_out.placements, symbols_by_ref) == []
    assert not state.results[-1].blocked


def test_mislabeled_net_blocks() -> None:
    plan = json.dumps({
        "placements": [
            {"ref": "U1", "x": 0, "y": 0},
            {"ref": "C1", "x": 30, "y": 0},
            {"ref": "R1", "x": 60, "y": 0},
        ],
        "label_nets": ["3V3", "GND", "NONEXISTENT_NET"],
    })
    state = _state()
    SchLayoutStep().run(state, PipelineContext(mode=LlmMode.AUTO, client=FakeLLM([plan])))
    result = state.results[-1]
    assert result.blocked
    assert any(c.name == "labels_match_netlist" and not c.ok for c in result.checks)


def test_unplaced_component_blocks() -> None:
    plan = json.dumps({
        "placements": [{"ref": "U1", "x": 0, "y": 0}, {"ref": "C1", "x": 30, "y": 0}],
        "label_nets": ["3V3"],
    })  # R1 missing
    state = _state()
    SchLayoutStep().run(state, PipelineContext(mode=LlmMode.AUTO, client=FakeLLM([plan])))
    result = state.results[-1]
    assert result.blocked
    assert any(c.name == "all_parts_placed" and not c.ok for c in result.checks)
