"""Task 7: pin mapping (logical -> real pin number), grounded in the symbol lib."""

from __future__ import annotations

import pytest

from ratsnestpro import config
from ratsnestpro.agents.llm import LlmMode
from ratsnestpro.eda import symbols
from ratsnestpro.orchestration.pipeline import (
    PipelineContext,
    PipelineState,
    PipelineStep,
    SchPinMapStep,
    _resolve_logical_pin,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    LogicalPin,
    MappedNet,
    MappedPin,
    NetIntent,
    NetlistIntent,
    PinMapPlan,
    SelectedPart,
    SelectionPlan,
)


@pytest.fixture(autouse=True)
def _libs(tmp_path, monkeypatch):
    symbols._load_lib_node.cache_clear()
    monkeypatch.setattr(config, "_ENV_LOADED", True)
    symdir = tmp_path / "T.kicad_symdir"
    symdir.mkdir()

    def lib(name: str, pins: str) -> str:
        return (
            f'(kicad_symbol_lib (version 20231120) (generator "t")'
            f'(symbol "{name}" (symbol "{name}_1_1" {pins})))'
        )

    def pin(t, n, num):
        return f'(pin {t} line (at 0 0 0) (length 2.54) (name "{n}") (number "{num}"))'

    (symdir / "LDO.kicad_sym").write_text(
        lib("LDO", pin("power_in", "IN", "1") + pin("power_out", "OUT", "2")
            + pin("power_in", "GND", "3") + pin("input", "EN", "4")),
        encoding="utf-8",
    )
    (symdir / "R.kicad_sym").write_text(
        lib("R", pin("passive", "~", "1") + pin("passive", "~", "2")), encoding="utf-8"
    )
    (symdir / "C.kicad_sym").write_text(
        lib("C", pin("passive", "~", "1") + pin("passive", "~", "2")), encoding="utf-8"
    )
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(tmp_path))
    yield
    symbols._load_lib_node.cache_clear()


def _state(nets: list[NetIntent]) -> PipelineState:
    s = PipelineState(requirement_text="x")
    s.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(ref="U1", symbol="T:LDO", value="ldo"),
        SelectedPart(ref="C1", symbol="T:C", value="1uF"),
        SelectedPart(ref="R1", symbol="T:R", value="10k"),
    ])
    s.artifacts[PipelineStep.SCH_CONNECTIONS] = NetlistIntent(
        nets=nets, supply_nets=["VIN"], ground_net="GND"
    )
    return s


def _lp(ref, pin):
    return LogicalPin(ref=ref, pin=pin)


def test_resolver_matches_number_name_and_token() -> None:
    pins = [
        {"number": "1", "name": "IN", "type": "power_in"},
        {"number": "7", "name": "XTAL1/PB6", "type": "bidirectional"},
    ]
    assert _resolve_logical_pin(pins, "1") == "1"       # by number
    assert _resolve_logical_pin(pins, "in") == "1"       # by name (case-insensitive)
    assert _resolve_logical_pin(pins, "XTAL1") == "7"    # token inside slash name
    assert _resolve_logical_pin(pins, "nope") is None


def test_resolver_matches_alphanumeric_pad_number_case_insensitively() -> None:
    pins = [{"number": "A1", "name": "GND", "type": "power_in"}]

    assert _resolve_logical_pin(pins, "A1") == "A1"
    assert _resolve_logical_pin(pins, "a1") == "A1"


def test_named_pins_map_to_real_numbers() -> None:
    nets = [
        NetIntent(name="VIN", kind="power", pins=[_lp("U1", "IN"), _lp("C1", "1")]),
        NetIntent(name="GND", kind="ground", pins=[_lp("U1", "GND"), _lp("C1", "2")]),
        NetIntent(name="OUT", kind="power", pins=[_lp("U1", "OUT"), _lp("R1", "1")]),
        NetIntent(name="EN", kind="signal", pins=[_lp("U1", "EN"), _lp("R1", "2")]),
    ]
    state = _state(nets)
    SchPinMapStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    plan = state.artifact(PipelineStep.SCH_PINMAP)
    assert isinstance(plan, PinMapPlan)
    vin = next(n for n in plan.nets if n.name == "VIN")
    numbers = {(p.ref, p.number) for p in vin.pins}
    assert ("U1", "1") in numbers  # IN -> pin 1
    gnd = next(n for n in plan.nets if n.name == "GND")
    assert ("U1", "3") in {(p.ref, p.number) for p in gnd.pins}  # GND -> pin 3
    result = state.results[-1]
    assert not result.blocked  # all LDO power pins connected -> no floating warning block


def test_unresolved_pin_blocks() -> None:
    nets = [
        NetIntent(name="VIN", kind="power", pins=[_lp("U1", "BOGUS"), _lp("C1", "1")]),
        NetIntent(name="GND", kind="ground", pins=[_lp("U1", "GND"), _lp("C1", "2")]),
    ]
    state = _state(nets)
    SchPinMapStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    result = state.results[-1]
    assert result.blocked
    assert any(c.name == "all_pins_resolved" and not c.ok for c in result.checks)


def test_double_assigned_pin_blocks() -> None:
    nets = [
        NetIntent(name="VIN", kind="power", pins=[_lp("U1", "IN"), _lp("C1", "1")]),
        NetIntent(name="OTHER", kind="signal", pins=[_lp("U1", "IN"), _lp("R1", "1")]),
        NetIntent(name="GND", kind="ground", pins=[_lp("U1", "GND"), _lp("C1", "2")]),
    ]
    state = _state(nets)
    SchPinMapStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    result = state.results[-1]
    assert result.blocked
    assert any(c.name == "no_double_assigned_pins" and not c.ok for c in result.checks)


def test_check_rejects_nonexistent_pin_number() -> None:
    state = _state([NetIntent(name="VIN", kind="power", pins=[_lp("U1", "IN")])])
    bogus = PinMapPlan(nets=[
        MappedNet(name="VIN", kind="power", pins=[MappedPin(ref="U1", logical="IN", number="99")])
    ])
    checks = SchPinMapStep().check(state, bogus)
    assert any(c.name == "mapped_pins_exist" and not c.ok for c in checks)
