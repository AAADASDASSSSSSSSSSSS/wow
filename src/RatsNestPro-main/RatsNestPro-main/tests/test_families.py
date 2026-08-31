"""Task 4: parametric Atmega328 family — params contract + IR/board builder."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ratsnestpro.families import Atmega328Params, build_ir, build_plan, expectations_for


def test_default_params_reproduce_reference() -> None:
    p = Atmega328Params()
    assert p.crystal_mhz == 16 and p.ldo_output_v == 5.0
    ir = build_ir(p)
    assert ir.family == "atmega328-dev-board"
    # 6 decoupling caps by default
    assert len(ir.components_with_role("decoupling")) == 6
    # power LED present
    assert ir.component("D1") is not None
    # two headers, four mounting holes
    assert len(ir.components_with_role("breakout_header")) == 2
    assert len(ir.components_with_role("mounting_hole")) == 4
    assert ir.component("U1").value == "AP22804AW5-7"
    assert ir.net("5V") is not None
    assert ir.net("3V3") is None


def test_16mhz_on_3v3_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Atmega328Params(crystal_mhz=16, ldo_output_v=3.3)


def test_8mhz_on_3v3_is_allowed_and_changes_board() -> None:
    p = Atmega328Params(crystal_mhz=8, ldo_output_v=3.3, power_led=False, mounting_holes=0)
    ir = build_ir(p)
    assert ir.component("D1") is None  # no power LED
    assert len(ir.components_with_role("mounting_hole")) == 0
    # 8 MHz load cap differs from 16 MHz
    loads = ir.components_with_role("crystal_load")
    assert all(c.value == "22pF" for c in loads)
    assert ir.component("U1").value == "MIC5504-3.3YM5-TR"
    assert ir.net("3V3") is not None


def test_load_cap_linkage() -> None:
    assert Atmega328Params(crystal_mhz=8, ldo_output_v=3.3).load_cap == "22pF"
    assert Atmega328Params(crystal_mhz=16, ldo_output_v=5.0).load_cap == "18pF"


def test_too_many_breakout_pins_rejected() -> None:
    with pytest.raises(ValidationError):
        Atmega328Params(breakout_rows=2, breakout_pins_per_row=12)  # 2*10=20 > 12 GPIO


def test_different_params_produce_different_boards() -> None:
    a = build_ir(Atmega328Params(crystal_mhz=16, ldo_output_v=5.0, decoupling_count=6))
    b = build_ir(
        Atmega328Params(
            crystal_mhz=8, ldo_output_v=3.3, decoupling_count=4, power_led=False,
            breakout_rows=1, mounting_holes=0,
        )
    )
    assert len(a.components) != len(b.components)
    assert a.component("Y1").value != b.component("Y1").value


def test_board_plan_places_every_component() -> None:
    p = Atmega328Params()
    ir = build_ir(p)
    plan = build_plan(p)
    assert {pl.ref for pl in plan.placements} == {c.ref for c in ir.components}
    coordinates = [(pl.x_mm, pl.y_mm) for pl in plan.placements]
    assert len(coordinates) == len(set(coordinates))


def test_expectations_track_params() -> None:
    p = Atmega328Params(crystal_mhz=8, ldo_output_v=3.3, decoupling_count=5)
    exp = expectations_for(p)
    assert exp.decoupling_count == 5
    assert exp.crystal_load_cap == "22pF"
    assert exp.supply_voltage_v == 3.3
    assert exp.supply_net == "3V3"
