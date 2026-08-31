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
    ComponentPrepareResult,
    FabAudit,
    ManufactureResult,
    PcbPlacement,
    PcbPlacementPlan,
    PcbWriteResult,
    PreparedComponent,
    RouteResult,
    SelectedPart,
    SelectionPlan,
)
from ratsnestpro.orchestration.review_project import _component_release_gate


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "_ENV_LOADED", True)
    # Force Gerber path to be treated as unavailable (deterministic offline test).
    monkeypatch.setattr(pl, "kicad_cli_available", lambda *a, **k: None)
    yield


def _state(tmp_path) -> PipelineState:
    pcb_path = tmp_path / "routed.kicad_pcb"
    pcb_path.write_text(
        """(kicad_pcb
  (version 20240108)
  (generator \"ratsnest-test\")
  (net 0 \"\")
  (net 1 \"SIG\")
  (footprint \"T:Part\"
    (layer \"F.Cu\")
    (at 10 10)
    (property \"Reference\" \"R1\" (at 0 0 0))
    (pad \"1\" smd rect (at 0 0) (size 1 1) (layers \"F.Cu\") (net 1 \"SIG\")))
  (footprint \"T:Part\"
    (layer \"F.Cu\")
    (at 20 10)
    (property \"Reference\" \"C1\" (at 0 0 0))
    (pad \"1\" smd rect (at 0 0) (size 1 1) (layers \"F.Cu\") (net 1 \"SIG\")))
  (segment (start 10 10) (end 20 10) (width 0.25) (layer \"F.Cu\") (net 1))
)\n""",
        encoding="utf-8",
    )
    dsn_path = tmp_path / "routed.dsn"
    ses_path = tmp_path / "routed.ses"
    dsn_path.write_text("(pcb routed)", encoding="utf-8")
    ses_path.write_text("(session routed)", encoding="utf-8")
    s = PipelineState(requirement_text="x", project_name="mfg")
    s.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(ref="R1", symbol="T:R", value="10k", footprint="T:Part", role="res"),
        SelectedPart(ref="C1", symbol="T:C", value="1uF", footprint="T:Part", role="dec"),
    ])
    s.artifacts[PipelineStep.LAYOUT_GENERAL] = PcbPlacementPlan(
        board_width=40, board_height=30,
        placements=[PcbPlacement(ref="R1", x=10, y=10), PcbPlacement(ref="C1", x=20, y=10)])
    s.artifacts[PipelineStep.LAYOUT_WRITE] = PcbWriteResult(
        pcb_path=str(pcb_path), component_count=2, overlaps=[], out_of_bounds=[])
    s.artifacts[PipelineStep.ROUTE_SIGNALS] = RouteResult(
        method="freerouting",
        required=True,
        routed_nets=1,
        total_nets=1,
        assigned_pads=2,
        routed_tracks=1,
        unconnected=0,
        dsn_path=str(dsn_path),
        ses_path=str(ses_path),
    )
    s.artifacts[PipelineStep.ROUTE_FAB] = FabAudit(violations=[])
    return s


def test_bom_and_cpl_written_and_drc_clean(tmp_path) -> None:
    state = _state(tmp_path)
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


def test_release_manifest_and_bom_match_standalone_review_contract(tmp_path) -> None:
    state = _state(tmp_path)
    state.artifacts[PipelineStep.COMPONENT_PREPARE] = ComponentPrepareResult(
        components=[
            PreparedComponent(
                ref=ref,
                symbol=f"T:{ref}",
                footprint="T:Part",
                mpn=f"MPN-{ref}",
                provider="jlcpcb",
                package_match="exact",
                datasheet="https://example.test/part.pdf",
                datasheet_status="verified_https",
                symbol_status="verified",
                footprint_status="verified",
                asset_status="verified",
                status="installed_exact",
                release_ready=True,
                unresolved=False,
                evidence=[f"catalog-snapshot:{ref}"],
            )
            for ref in ("R1", "C1")
        ],
        release_ready=True,
    )

    ManufactureStep().run(
        state,
        PipelineContext(mode=LlmMode.OFFLINE, out_dir=str(tmp_path)),
    )

    manifest = json.loads(
        (tmp_path / "mfg_component_release.json").read_text(encoding="utf-8")
    )
    assert manifest["schema_version"] == 2
    assert manifest["release_ready"] is True
    assert manifest["release_proven_component_count"] == 2
    bom = (tmp_path / "mfg_bom.csv").read_text(encoding="utf-8")
    assert "ReleaseReady" in bom
    assert "Resolution" in bom
    release_gate = _component_release_gate(tmp_path, None, None)
    assert release_gate.status.value == "passed"
    assert not release_gate.required


def test_drc_violation_blocks(tmp_path) -> None:
    state = _state(tmp_path)
    state.artifacts[PipelineStep.LAYOUT_WRITE] = PcbWriteResult(
        pcb_path="x.kicad_pcb", component_count=2, overlaps=["R1&C1"], out_of_bounds=[])
    ManufactureStep().run(state, PipelineContext(mode=LlmMode.OFFLINE, out_dir=str(tmp_path)))
    result = state.results[-1]
    assert result.blocked
    assert any(c.name == "drc_clean" and not c.ok for c in result.checks)


def test_fab_violation_surfaces_in_drc(tmp_path) -> None:
    state = _state(tmp_path)
    state.artifacts[PipelineStep.ROUTE_FAB] = FabAudit(violations=["signal: width too small"])
    ManufactureStep().run(state, PipelineContext(mode=LlmMode.OFFLINE, out_dir=str(tmp_path)))
    result = state.results[-1]
    assert result.blocked
    art = state.artifact(PipelineStep.MANUFACTURE)
    assert any("fab:" in v for v in art.drc_violations)


def test_incomplete_routing_blocks_release_and_never_exports_gerber(
    tmp_path,
    monkeypatch,
) -> None:
    state = _state(tmp_path)
    state.artifacts[PipelineStep.ROUTE_SIGNALS] = RouteResult(
        method="deferred",
        required=False,
        routed_nets=0,
        total_nets=1,
        assigned_pads=0,
        routed_tracks=0,
        unconnected=-1,
    )
    gerber_attempted = False

    def fake_cli(*_args, **_kwargs):
        return "kicad-cli"

    def fake_run(*_args, **_kwargs):
        nonlocal gerber_attempted
        gerber_attempted = True
        raise AssertionError("manufacture must not call KiCad when routing is incomplete")

    monkeypatch.setattr(pl, "kicad_cli_available", fake_cli)
    monkeypatch.setattr(pl, "_run_kicad_drc", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("subprocess.run", fake_run)

    ManufactureStep().run(
        state,
        PipelineContext(mode=LlmMode.OFFLINE, out_dir=str(tmp_path)),
    )

    art = state.artifact(PipelineStep.MANUFACTURE)
    assert isinstance(art, ManufactureResult)
    assert state.results[-1].blocked
    assert not art.gerber_exported
    assert not art.gerber_dir
    assert not gerber_attempted
    assert any("routing:" in violation for violation in art.drc_violations)


def test_route_metadata_cannot_hide_missing_pad_net_assignments(tmp_path) -> None:
    state = _state(tmp_path)
    write_result = state.artifact(PipelineStep.LAYOUT_WRITE)
    assert isinstance(write_result, PcbWriteResult)
    pcb_path = tmp_path / "routed.kicad_pcb"
    pcb_path.write_text(
        """(kicad_pcb
  (version 20240108)
  (generator \"ratsnest-test\")
  (net 0 \"\")
  (net 1 \"SIG\")
  (footprint \"T:Part\" (layer \"F.Cu\") (at 10 10)
    (property \"Reference\" \"R1\" (at 0 0 0))
    (pad \"1\" smd rect (at 0 0) (size 1 1) (layers \"F.Cu\")))
  (segment (start 10 10) (end 20 10) (width 0.25) (layer \"F.Cu\") (net 1))
)\n""",
        encoding="utf-8",
    )

    ManufactureStep().run(
        state,
        PipelineContext(mode=LlmMode.OFFLINE, out_dir=str(tmp_path)),
    )

    art = state.artifact(PipelineStep.MANUFACTURE)
    assert isinstance(art, ManufactureResult)
    assert state.results[-1].blocked
    assert any("pads have no electrical net" in item for item in art.drc_violations)


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
