"""Task 19: DRC bottom-line + manufacturing outputs (BOM/CPL/Gerber)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ratsnestpro import config
from ratsnestpro.agents.llm import LlmMode
from ratsnestpro.orchestration import pipeline as pl
from ratsnestpro.orchestration.pipeline import (
    ManufactureStep,
    PipelineContext,
    PipelineState,
    PipelineStep,
    _run_kicad_drc,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    FabAudit,
    ManufactureResult,
    PcbPlacement,
    PcbPlacementPlan,
    PcbWriteResult,
    SelectedPart,
    SelectionPlan,
)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "_ENV_LOADED", True)
    # Force Gerber path to be treated as unavailable (deterministic offline test).
    monkeypatch.setattr(pl, "kicad_cli_available", lambda *a, **k: None)
    yield


def _state() -> PipelineState:
    s = PipelineState(requirement_text="x", project_name="mfg")
    s.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(ref="R1", symbol="T:R", value="10k", footprint="T:Part", role="res"),
        SelectedPart(ref="C1", symbol="T:C", value="1uF", footprint="T:Part", role="dec"),
    ])
    s.artifacts[PipelineStep.LAYOUT_GENERAL] = PcbPlacementPlan(
        board_width=40, board_height=30,
        placements=[PcbPlacement(ref="R1", x=10, y=10), PcbPlacement(ref="C1", x=20, y=10)])
    s.artifacts[PipelineStep.LAYOUT_WRITE] = PcbWriteResult(
        pcb_path="x.kicad_pcb", component_count=2, overlaps=[], out_of_bounds=[])
    s.artifacts[PipelineStep.ROUTE_FAB] = FabAudit(violations=[])
    return s


def test_bom_and_cpl_written_and_drc_clean(tmp_path) -> None:
    state = _state()
    ManufactureStep().run(state, PipelineContext(mode=LlmMode.OFFLINE, out_dir=str(tmp_path)))
    art = state.artifact(PipelineStep.MANUFACTURE)
    assert isinstance(art, ManufactureResult)
    assert (tmp_path / "mfg_bom.csv").exists()
    assert (tmp_path / "mfg_cpl.csv").exists()
    bom = (tmp_path / "mfg_bom.csv").read_text(encoding="utf-8")
    assert "R1" in bom and "C1" in bom
    result = state.results[-1]
    assert not result.blocked  # gerber-unavailable is a warning, not a block
    assert any(c.name == "gerber_exported" and not c.ok for c in result.checks)


def test_drc_violation_blocks(tmp_path) -> None:
    state = _state()
    state.artifacts[PipelineStep.LAYOUT_WRITE] = PcbWriteResult(
        pcb_path="x.kicad_pcb", component_count=2, overlaps=["R1&C1"], out_of_bounds=[])
    ManufactureStep().run(state, PipelineContext(mode=LlmMode.OFFLINE, out_dir=str(tmp_path)))
    result = state.results[-1]
    assert result.blocked
    assert any(c.name == "drc_clean" and not c.ok for c in result.checks)


def test_fab_violation_surfaces_in_drc(tmp_path) -> None:
    state = _state()
    state.artifacts[PipelineStep.ROUTE_FAB] = FabAudit(violations=["signal: width too small"])
    ManufactureStep().run(state, PipelineContext(mode=LlmMode.OFFLINE, out_dir=str(tmp_path)))
    result = state.results[-1]
    assert result.blocked
    art = state.artifact(PipelineStep.MANUFACTURE)
    assert any("fab:" in v for v in art.drc_violations)


def test_final_kicad_drc_returns_only_error_severity(
    tmp_path,
    monkeypatch,
) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    report = tmp_path / "board.drc.json"

    def fake_run(args, **_kwargs):
        output = args[args.index("--output") + 1]
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "violations": [
                        {
                            "type": "via_diameter",
                            "severity": "error",
                            "description": "via too small",
                        },
                        {
                            "type": "silk_overlap",
                            "severity": "warning",
                            "description": "silk overlap",
                        },
                    ],
                    "unconnected_items": [],
                },
                handle,
            )
        return SimpleNamespace(returncode=5, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert _run_kicad_drc("kicad-cli", pcb, report) == [
        "kicad_cli:via_diameter:via too small"
    ]
