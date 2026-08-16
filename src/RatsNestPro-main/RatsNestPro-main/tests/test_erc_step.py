"""Task 10: schematic ERC bottom-line (deterministic + optional cli ERC)."""

from __future__ import annotations

import pytest

from ratsnestpro import config
from ratsnestpro.agents.llm import LlmMode
from ratsnestpro.eda import symbols
from ratsnestpro.eda.adapter import ErcResult
from ratsnestpro.orchestration import pipeline as pl
from ratsnestpro.orchestration.pipeline import (
    ErcStep,
    PipelineContext,
    PipelineState,
    PipelineStep,
    SchMaterializeStep,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    ErcSummary,
    MappedNet,
    MappedPin,
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

    def pin(num, x, y):
        return f'(pin passive line (at {x} {y} 0) (length 2.54) (name "~") (number "{num}"))'

    (symdir / "R.kicad_sym").write_text(lib("R", pin("1", 0, 3.81) + pin("2", 0, -3.81)),
                                        encoding="utf-8")
    (symdir / "C.kicad_sym").write_text(lib("C", pin("1", 0, 2.54) + pin("2", 0, -2.54)),
                                        encoding="utf-8")
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(tmp_path))
    yield
    symbols._load_lib_node.cache_clear()


def _materialize(tmp_path, pinmap: PinMapPlan) -> PipelineState:
    s = PipelineState(requirement_text="x", project_name="erc")
    s.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(ref="R1", symbol="T:R", value="10k"),
        SelectedPart(ref="C1", symbol="T:C", value="1uF"),
    ])
    s.artifacts[PipelineStep.SCH_CONNECTIONS] = NetlistIntent(
        nets=[NetIntent(name="N1", kind="signal", pins=[])], supply_nets=["N1"], ground_net="GND",
    )
    s.artifacts[PipelineStep.SCH_LAYOUT] = SchLayoutPlan(placements=[
        SheetPlacement(ref="R1", x=50, y=50), SheetPlacement(ref="C1", x=80, y=50)])
    s.artifacts[PipelineStep.SCH_PINMAP] = pinmap
    SchMaterializeStep().run(s, PipelineContext(mode=LlmMode.OFFLINE, out_dir=str(tmp_path)))
    return s


def _fake_erc(available: bool, errors: int = 0):
    def _run(sch_path, out_dir=None, explicit_cli=None):
        return ErcResult(available=available, ran=available, ok=(errors == 0),
                         error_count=errors, warning_count=0)
    return _run


def test_clean_schematic_passes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pl, "run_erc", _fake_erc(available=True, errors=0))
    pinmap = PinMapPlan(nets=[MappedNet(name="N1", kind="signal", pins=[
        MappedPin(ref="R1", logical="1", number="1"),
        MappedPin(ref="C1", logical="1", number="1")])])
    state = _materialize(tmp_path, pinmap)
    ErcStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    result = state.results[-1]
    assert not result.blocked
    summ = state.artifact(PipelineStep.ERC)
    assert isinstance(summ, ErcSummary)
    assert summ.cli_available and summ.cli_error_count == 0


def test_single_pin_net_blocks(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pl, "run_erc", _fake_erc(available=True, errors=0))
    pinmap = PinMapPlan(nets=[MappedNet(name="LONELY", kind="signal", pins=[
        MappedPin(ref="R1", logical="1", number="1")])])
    state = _materialize(tmp_path, pinmap)
    ErcStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    result = state.results[-1]
    assert result.blocked
    assert any(c.name == "no_single_pin_nets" and not c.ok for c in result.checks)


def test_cli_unavailable_is_warning_not_pass(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pl, "run_erc", _fake_erc(available=False))
    pinmap = PinMapPlan(nets=[MappedNet(name="N1", kind="signal", pins=[
        MappedPin(ref="R1", logical="1", number="1"),
        MappedPin(ref="C1", logical="1", number="1")])])
    state = _materialize(tmp_path, pinmap)
    ErcStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    result = state.results[-1]
    # Deterministic checks pass -> not blocked, but ERC availability is a warning.
    assert not result.blocked
    erc_check = next(c for c in result.checks if c.name == "kicad_cli_erc")
    assert not erc_check.ok and erc_check.severity.value == "warning"


def test_cli_errors_block_the_pipeline(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pl, "run_erc", _fake_erc(available=True, errors=3))
    pinmap = PinMapPlan(nets=[MappedNet(name="N1", kind="signal", pins=[
        MappedPin(ref="R1", logical="1", number="1"),
        MappedPin(ref="C1", logical="1", number="1")])])
    state = _materialize(tmp_path, pinmap)
    ErcStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    result = state.results[-1]
    assert result.blocked
    erc_check = next(c for c in result.checks if c.name == "kicad_cli_erc")
    assert not erc_check.ok and erc_check.severity.value == "error"
