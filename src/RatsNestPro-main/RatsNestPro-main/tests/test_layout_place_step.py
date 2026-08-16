"""Tasks 12-14: critical placement, general placement + alignment, write .kicad_pcb."""

from __future__ import annotations

import pytest

from ratsnestpro import config
from ratsnestpro.agents.llm import LlmMode
from ratsnestpro.orchestration.pipeline import (
    LayoutCriticalStep,
    LayoutGeneralStep,
    LayoutWriteStep,
    PipelineContext,
    PipelineState,
    PipelineStep,
    _rotated_bbox,
    _zone_targets,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    BoardPartition,
    BoardZone,
    PcbPlacement,
    PcbPlacementPlan,
    PcbWriteResult,
    SelectedPart,
    SelectionPlan,
)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.setattr(config, "_ENV_LOADED", True)
    config._load_capability.cache_clear()
    yield
    config._load_capability.cache_clear()


def _state(footprint_lib: str = "") -> PipelineState:
    s = PipelineState(requirement_text="x", project_name="lay")
    fp = f"{footprint_lib}:Part" if footprint_lib else "Package_QFP:TQFP-32_7x7mm_P0.8mm"
    s.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(ref="U2", symbol="T:MCU", value="mcu", footprint=fp, role="mcu"),
        SelectedPart(ref="Y1", symbol="T:X", value="8MHz", footprint=fp, role="crystal"),
        SelectedPart(ref="C1", symbol="T:C", value="22pF", footprint=fp, role="crystal_load"),
        SelectedPart(ref="C3", symbol="T:C", value="100nF", footprint=fp, role="decoupling"),
        SelectedPart(ref="U1", symbol="T:LDO", value="ldo", footprint=fp, role="ldo"),
        SelectedPart(ref="J1", symbol="T:USB", value="usb", footprint=fp, role="power_input"),
        SelectedPart(ref="J2", symbol="T:H", value="hdr", footprint=fp, role="breakout_header"),
        SelectedPart(ref="R3", symbol="T:R", value="10k", footprint=fp, role="reset_pullup"),
    ])
    s.artifacts[PipelineStep.LAYOUT_PARTITION] = BoardPartition(
        board_width=70.0, board_height=50.0, zones=[])
    return s


def test_rotated_bbox_uses_kicad_clockwise_coordinates() -> None:
    assert _rotated_bbox((-1.0, -2.0, 3.0, 4.0), 90) == pytest.approx(
        (-2.0, -3.0, 4.0, 1.0)
    )


def test_critical_placement_satisfies_constraints() -> None:
    state = _state()
    LayoutCriticalStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    result = state.results[-1]
    assert not result.blocked
    assert all(c.ok for c in result.checks)
    plan = state.artifact(PipelineStep.LAYOUT_CRITICAL)
    assert isinstance(plan, PcbPlacementPlan)
    assert {"U2", "Y1", "C3", "J1", "J2"} <= {p.ref for p in plan.placements}


def test_critical_placement_recognizes_semantic_role_variants() -> None:
    state = PipelineState(requirement_text="x", project_name="role-variants")
    footprint = "Package_QFP:TQFP-32_7x7mm_P0.8mm"
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="T:MCU",
            value="mcu",
            footprint=footprint,
            role="main_mcu",
        ),
        SelectedPart(
            ref="Y1",
            symbol="T:X",
            value="12MHz",
            footprint=footprint,
            role="xtal_12mhz",
        ),
        SelectedPart(
            ref="C1",
            symbol="T:C",
            value="100n",
            footprint=footprint,
            role="mcu_vdd_decoupling_1",
        ),
        SelectedPart(
            ref="J1",
            symbol="T:USB",
            value="usb",
            footprint=footprint,
            role="usb_c_connector",
        ),
    ])
    state.artifacts[PipelineStep.LAYOUT_PARTITION] = BoardPartition(
        board_width=60.0,
        board_height=40.0,
        zones=[],
    )

    LayoutCriticalStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    plan = state.artifact(PipelineStep.LAYOUT_CRITICAL)
    assert isinstance(plan, PcbPlacementPlan)
    assert {placement.ref for placement in plan.placements} == {
        "U1",
        "Y1",
        "C1",
        "J1",
    }


def test_critical_placement_scales_for_many_mcu_decouplers() -> None:
    state = PipelineState(requirement_text="x", project_name="many-decouplers")
    footprint = "Capacitor_SMD:C_0603_1608Metric"
    parts = [
        SelectedPart(
            ref="U1",
            symbol="T:MCU",
            value="mcu",
            footprint="Package_DFN_QFN:QFN-56-1EP_7x7mm_P0.4mm_EP3.2x3.2mm",
            role="main_mcu",
        )
    ]
    parts.extend(
        SelectedPart(
            ref=f"C{index}",
            symbol="Device:C",
            value="100n",
            footprint=footprint,
            role=f"mcu_vdd_decoupling_{index}",
        )
        for index in range(1, 25)
    )
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=parts)
    state.artifacts[PipelineStep.LAYOUT_PARTITION] = BoardPartition(
        board_width=60.0,
        board_height=40.0,
        zones=[],
    )

    LayoutCriticalStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    result = state.results[-1]
    plan = state.artifact(PipelineStep.LAYOUT_CRITICAL)
    assert not result.blocked
    assert isinstance(plan, PcbPlacementPlan)
    assert len(plan.placements) == 25


def test_critical_placement_keeps_close_memory_with_controller() -> None:
    state = PipelineState(requirement_text="x", project_name="memory")
    footprint = "Package_QFP:TQFP-32_7x7mm_P0.8mm"
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="T:MCU",
            value="mcu",
            footprint=footprint,
            role="main_mcu",
        ),
        SelectedPart(
            ref="U2",
            symbol="Memory_Flash:W25Q128JVS",
            value="flash",
            footprint=footprint,
            role="external_qspi_flash",
        ),
    ])
    state.artifacts[PipelineStep.LAYOUT_PARTITION] = BoardPartition(
        board_width=60.0,
        board_height=40.0,
        zones=[],
    )

    LayoutCriticalStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    plan = state.artifact(PipelineStep.LAYOUT_CRITICAL)
    assert isinstance(plan, PcbPlacementPlan)
    by_ref = plan.by_ref()
    assert {"U1", "U2"} <= set(by_ref)
    assert (
        (by_ref["U1"].x - by_ref["U2"].x) ** 2
        + (by_ref["U1"].y - by_ref["U2"].y) ** 2
    ) ** 0.5 <= 20.0


def test_critical_placement_deduplicates_overlapping_semantic_roles() -> None:
    state = PipelineState(requirement_text="x", project_name="overlapping-roles")
    footprint = "Capacitor_SMD:C_0603_1608Metric"
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="T:MCU",
            value="mcu",
            footprint=footprint,
            role="main_mcu",
        ),
        SelectedPart(
            ref="C1",
            symbol="Device:C",
            value="100n",
            footprint=footprint,
            role="qspi_flash_decoupling",
        ),
    ])
    state.artifacts[PipelineStep.LAYOUT_PARTITION] = BoardPartition(
        board_width=60.0,
        board_height=40.0,
        zones=[],
    )

    LayoutCriticalStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    result = state.results[-1]
    plan = state.artifact(PipelineStep.LAYOUT_CRITICAL)
    assert not result.blocked
    assert isinstance(plan, PcbPlacementPlan)
    assert [placement.ref for placement in plan.placements].count("C1") == 1


def test_general_placement_places_all_inside_fixed_outline() -> None:
    state = _state()
    LayoutCriticalStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    LayoutGeneralStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    result = state.results[-1]
    assert not result.blocked
    plan = state.artifact(PipelineStep.LAYOUT_GENERAL)
    assert isinstance(plan, PcbPlacementPlan)
    assert {p.ref for p in plan.placements} == {"U2", "Y1", "C1", "C3", "U1", "J1", "J2", "R3"}
    assert (plan.board_width, plan.board_height) == (70.0, 50.0)
    for p in plan.placements:
        assert 0.0 <= p.x <= plan.board_width
        assert 0.0 <= p.y <= plan.board_height
        assert p.rotation in (0.0, 90.0, 180.0, 270.0)


def test_general_placement_prioritizes_mcu_before_large_connectors() -> None:
    state = PipelineState(requirement_text="x", project_name="priority")
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="T:MCU",
            value="mcu",
            footprint="Package_DFN_QFN:QFN-56-1EP_7x7mm_P0.4mm_EP3.2x3.2mm",
            role="main_mcu",
        ),
        SelectedPart(
            ref="U2",
            symbol="T:Flash",
            value="flash",
            footprint="Package_SON:WSON-8-1EP_8x6mm_P1.27mm_EP3.4x4.3mm",
            role="qspi_flash",
        ),
        SelectedPart(
            ref="J1",
            symbol="T:USB",
            value="usb",
            footprint="Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
            role="usb_c_connector",
        ),
        *[
            SelectedPart(
                ref=f"C{index}",
                symbol="Device:C",
                value="100n",
                footprint="Capacitor_SMD:C_0603_1608Metric",
                role=f"local_filter_{index}",
            )
            for index in range(1, 27)
        ],
    ])
    state.artifacts[PipelineStep.LAYOUT_PARTITION] = BoardPartition(
        board_width=60.0,
        board_height=40.0,
        zones=[],
    )
    state.artifacts[PipelineStep.LAYOUT_CRITICAL] = PcbPlacementPlan(
        board_width=60.0,
        board_height=40.0,
        placements=[
            PcbPlacement(ref="U1", x=30.0, y=20.0),
            PcbPlacement(ref="U2", x=38.0, y=20.0),
            PcbPlacement(ref="J1", x=5.0, y=20.0),
        ],
    )

    LayoutGeneralStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    result = state.results[-1]
    plan = state.artifact(PipelineStep.LAYOUT_GENERAL)
    assert not result.blocked
    assert isinstance(plan, PcbPlacementPlan)
    assert len(plan.placements) == 29
    assert {"U1", "U2", "J1"} <= set(plan.by_ref())


def test_general_placement_respects_functional_zone_targets() -> None:
    state = PipelineState(requirement_text="x", project_name="zoned")
    footprint = "Package_QFP:TQFP-32_7x7mm_P0.8mm"
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="T:MCU",
            value="mcu",
            footprint=footprint,
            role="mcu",
        ),
        SelectedPart(
            ref="U2",
            symbol="T:BUCK",
            value="buck",
            footprint=footprint,
            role="buck_regulator",
        ),
    ])
    state.artifacts[PipelineStep.LAYOUT_PARTITION] = BoardPartition(
        board_width=60.0,
        board_height=30.0,
        zones=[
            BoardZone(name="Power", kind="power", x1=0, y1=0, x2=20, y2=30),
            BoardZone(name="MCU", kind="digital", x1=40, y1=0, x2=60, y2=30),
        ],
    )

    LayoutGeneralStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    plan = state.artifact(PipelineStep.LAYOUT_GENERAL)
    assert isinstance(plan, PcbPlacementPlan)
    by_ref = plan.by_ref()
    assert by_ref["U2"].x < by_ref["U1"].x


def test_zone_targets_match_unfamiliar_roles_semantically() -> None:
    state = PipelineState(requirement_text="x", project_name="semantic-zones")
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="Interface_Ethernet:LAN8720A",
            value="LAN8720A",
            footprint="Package_QFP:LQFP-48_7x7mm_P0.5mm",
            role="ethernet_phy",
        ),
        SelectedPart(
            ref="U2",
            symbol="Driver_Motor:DRV8876",
            value="DRV8876",
            footprint="Package_SO:HTSSOP-16-1EP_4.4x5mm_P0.65mm",
            role="motor_driver",
        ),
    ])
    state.artifacts[PipelineStep.LAYOUT_PARTITION] = BoardPartition(
        board_width=60.0,
        board_height=30.0,
        zones=[
            BoardZone(
                name="Ethernet PHY",
                kind="ethernet",
                x1=0,
                y1=0,
                x2=20,
                y2=30,
            ),
            BoardZone(
                name="Motor drive",
                kind="motor",
                x1=40,
                y1=0,
                x2=60,
                y2=30,
            ),
        ],
    )

    assert _zone_targets(state) == {
        "U1": (10.0, 15.0),
        "U2": (50.0, 15.0),
    }


def test_write_without_footprint_geometry_warns_not_blocks(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    # Simulate truly unavailable footprint geometry (independent of any installed
    # KiCad libraries on the test machine).
    from ratsnestpro.orchestration import pipeline as pl
    monkeypatch.setattr(pl.footprints, "footprint_bbox", lambda lib_id: None)
    monkeypatch.setattr(pl.footprints, "footprint_path", lambda lib_id: None)
    state = _state()
    LayoutCriticalStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    LayoutGeneralStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    LayoutWriteStep().run(state, PipelineContext(mode=LlmMode.OFFLINE, out_dir=str(tmp_path)))
    result = state.results[-1]
    art = state.artifact(PipelineStep.LAYOUT_WRITE)
    assert isinstance(art, PcbWriteResult)
    assert (tmp_path / "lay.kicad_pcb").exists()
    assert (tmp_path / "lay.unrouted.kicad_pcb").exists()
    # No footprint geometry -> courtyard checks can't run -> not blocked.
    assert not result.blocked
    assert not art.overlaps and not art.out_of_bounds


def test_write_overlap_check_unit() -> None:
    state = _state()
    checks = LayoutWriteStep().check(
        state, PcbWriteResult(pcb_path="x.kicad_pcb", overlaps=["A&B"], out_of_bounds=[]))
    assert any(c.name == "no_courtyard_overlap" and not c.ok for c in checks)
    checks2 = LayoutWriteStep().check(
        state, PcbWriteResult(pcb_path="x.kicad_pcb", overlaps=[], out_of_bounds=["Z"]))
    assert any(c.name == "within_board" and not c.ok for c in checks2)


def test_write_with_fixture_footprint(tmp_path, monkeypatch) -> None:
    pretty = tmp_path / "T.pretty"
    pretty.mkdir()
    (pretty / "Part.kicad_mod").write_text(
        '(footprint "Part" (layer "F.Cu")'
        '(pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu"))'
        '(pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu")))',
        encoding="utf-8",
    )
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(tmp_path))

    # Two well-separated parts -> no overlap; board written.
    state = PipelineState(requirement_text="x", project_name="fx")
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(ref="R1", symbol="T:R", value="1k", footprint="T:Part", role="res"),
        SelectedPart(ref="R2", symbol="T:R", value="1k", footprint="T:Part", role="res"),
    ])
    state.artifacts[PipelineStep.LAYOUT_PARTITION] = BoardPartition(
        board_width=40.0, board_height=30.0, zones=[])
    state.artifacts[PipelineStep.LAYOUT_GENERAL] = PcbPlacementPlan(
        board_width=40.0, board_height=30.0, placements=[
            PcbPlacement(ref="R1", x=10, y=15), PcbPlacement(ref="R2", x=30, y=15)])
    LayoutWriteStep().run(state, PipelineContext(mode=LlmMode.OFFLINE, out_dir=str(tmp_path)))
    result = state.results[-1]
    assert not result.blocked
    assert (tmp_path / "fx.kicad_pcb").exists()
