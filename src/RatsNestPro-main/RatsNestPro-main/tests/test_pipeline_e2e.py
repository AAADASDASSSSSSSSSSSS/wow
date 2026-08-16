"""Task 20: end-to-end pipeline integration (offline; graceful without KiCad)."""

from __future__ import annotations

import pytest

from ratsnestpro import config
from ratsnestpro.agents.llm import LlmMode
from ratsnestpro.eda import routing
from ratsnestpro.eda.adapter import ErcResult
from ratsnestpro.families import Atmega328Params, build_ir
from ratsnestpro.orchestration import pipeline as pl
from ratsnestpro.orchestration.pipeline import (
    ALL_STEPS,
    Pipeline,
    PipelineContext,
    PipelineState,
    PipelineStep,
    _classify_net,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    LogicalPin,
    NetIntent,
    NetlistIntent,
    SelectedPart,
    SelectionPlan,
)


@pytest.fixture(autouse=True)
def _deterministic(monkeypatch):
    # Make external EDA tools deterministic-unavailable so the offline flow is
    # reproducible regardless of whether KiCad, kicad-cli or Freerouting is
    # installed on the test host. Freerouting matters most: when it *is*
    # installed a single run takes half an hour and its result varies.
    monkeypatch.setattr(config, "_ENV_LOADED", True)
    # Symbol/footprint discovery has to be stubbed explicitly, not just left
    # unconfigured. ``config.symbol_dir`` falls back to any KiCad install it can
    # find, so on a host with KiCad this test would otherwise run the real
    # pin-level checks against a fixture whose purpose is only to exercise step
    # ORDER and plumbing — making the outcome depend on the host after all.
    monkeypatch.setattr(config, "_first_discovered", lambda kind: None)
    monkeypatch.setattr(pl, "run_erc",
                        lambda *a, **k: ErcResult(available=False, ran=False, ok=False))
    monkeypatch.setattr(pl, "kicad_cli_available", lambda *a, **k: None)
    monkeypatch.setattr(routing, "available", lambda: False)
    monkeypatch.setattr(
        routing,
        "autoroute",
        lambda *a, **k: routing.RouteOutcome(
            method="deferred", ok=False, layers=2, nets=0, assigned_pads=0,
            routed_tracks=0, unconnected=0,
            note="autorouter disabled for a deterministic test run",
        ),
    )
    yield


def _reference_design() -> tuple[SelectionPlan, NetlistIntent]:
    """The ATmega328 reference board as a *test fixture*, not a production default.

    Selection and connections used to substitute this design whenever a step had
    no LLM proposal, which silently produced an ATmega board for a non-ATmega
    requirement. That substitution is gone from the pipeline; the design is still
    the cheapest known-good input for exercising the 13 steps that follow.
    """
    ir = build_ir(Atmega328Params(crystal_mhz=8, ldo_output_v=3.3))
    parts = [
        SelectedPart(
            ref=c.ref, symbol=c.symbol, value=c.value,
            footprint=c.footprint, role=c.role,
        )
        for c in ir.components
    ]
    nets = [
        NetIntent(
            name=n.name,
            kind=_classify_net(n.name),
            pins=[LogicalPin(ref=p.component_ref, pin=p.pin) for p in n.pins],
            purpose=str(n.properties.get("purpose", "")),
        )
        for n in ir.nets
    ]
    return (
        SelectionPlan(parts=parts, rationale="test fixture: reference BOM"),
        NetlistIntent(
            nets=nets,
            supply_nets=[n.name for n in nets if n.kind == "power"],
            ground_net="GND",
            # The family states which pins it leaves open. Dropping that here
            # would make the fixture claim those pins were forgotten rather than
            # unused by decision, which is exactly the distinction the pin
            # coverage check exists to draw.
            no_connect_pins=[
                LogicalPin(ref=p.component_ref, pin=p.pin)
                for p in ir.no_connect_pins
            ],
            rationale="test fixture: reference connectivity",
        ),
    )


class _ReferenceDesignLLM:
    """Answers only the two steps that need a design proposal.

    Anything else gets unparseable output, so those steps take their
    deterministic fallback exactly as they do offline. This replaces the removed
    behaviour where selection and connections silently substituted this design.
    """

    def __init__(self) -> None:
        selection, intent = _reference_design()
        self._selection = selection.model_dump_json()
        self._intent = intent.model_dump_json()

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        if system.startswith("You choose real components"):
            return self._selection
        if system.startswith("You design the electrical connectivity"):
            return self._intent
        return "no proposal"


def _context(tmp_path) -> PipelineContext:
    return PipelineContext(
        mode=LlmMode.AUTO, client=_ReferenceDesignLLM(), out_dir=str(tmp_path)
    )


def test_full_pipeline_reaches_manufacture(tmp_path) -> None:
    state = PipelineState(requirement_text="ATmega328 USB-C 3.3V 8MHz dev board",
                          project_name="e2e")
    Pipeline().run(state, _context(tmp_path))
    completed = set(state.completed)
    # The whole fixed flow runs end-to-end and reaches manufacturing.
    assert PipelineStep.MANUFACTURE in completed
    assert len(state.results) == len(ALL_STEPS)
    assert not state.blocked
    # Deliverables written.
    assert (tmp_path / "e2e.kicad_sch").exists()
    assert (tmp_path / "e2e.kicad_pcb").exists()
    assert (tmp_path / "e2e_bom.csv").exists()
    assert (tmp_path / "e2e_cpl.csv").exists()


def test_full_pipeline_order_matches_canonical(tmp_path) -> None:
    state = PipelineState(requirement_text="ATmega328 USB-C 3.3V 8MHz dev board",
                          project_name="ord")
    Pipeline().run(state, _context(tmp_path))
    assert state.completed == [s.step for s in ALL_STEPS]


def test_no_proposal_blocks_instead_of_substituting_a_design(tmp_path) -> None:
    """Offline means no design at all — never a different board's design."""
    state = PipelineState(requirement_text="STM32F103 USB sensor node", project_name="off")
    Pipeline().run(state, PipelineContext(mode=LlmMode.OFFLINE, out_dir=str(tmp_path)))
    assert state.blocked
    assert PipelineStep.MANUFACTURE not in set(state.completed)
    selection = state.artifact(PipelineStep.SELECTION)
    assert isinstance(selection, SelectionPlan)
    assert selection.parts == []
