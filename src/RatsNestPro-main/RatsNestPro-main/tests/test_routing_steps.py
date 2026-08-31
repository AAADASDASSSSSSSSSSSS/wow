"""Tasks 15-18: routing plan, planes, signal routing (degraded), fab audit."""

from __future__ import annotations

import json

import pytest

from ratsnestpro import config
from ratsnestpro.agents.llm import LlmMode
from ratsnestpro.eda import routing
from ratsnestpro.eda.routing import RouteOutcome
from ratsnestpro.orchestration.pipeline import (
    PipelineContext,
    PipelineState,
    PipelineStep,
    RouteFabStep,
    RoutePlanesStep,
    RoutePlanStep,
    RouteSignalsStep,
    _copper_layer_tokens,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    FabAudit,
    LogicalPin,
    MappedNet,
    MappedPin,
    NetClass,
    NetIntent,
    NetlistIntent,
    PcbWriteResult,
    PinMapPlan,
    PlanePlan,
    RoutePlan,
    RouteResult,
)


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def complete(self, system, user, temperature=0.2):
        return self._responses.pop(0) if self._responses else "{}"


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "_ENV_LOADED", True)
    config._load_capability.cache_clear()
    yield
    config._load_capability.cache_clear()


def test_route_plan_meets_process_minimums() -> None:
    state = PipelineState(requirement_text="x")
    RoutePlanStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    plan = state.artifact(PipelineStep.ROUTE_PLAN)
    assert isinstance(plan, RoutePlan)
    result = state.results[-1]
    assert not result.blocked
    assert all(c.ok for c in result.checks)


def test_route_plan_honors_four_layer_requirement() -> None:
    state = PipelineState(
        requirement_text="Use a four-layer PCB with a continuous ground plane"
    )
    RoutePlanStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    plan = state.artifact(PipelineStep.ROUTE_PLAN)
    assert isinstance(plan, RoutePlan)
    assert plan.layers == 4


def test_net_class_normalizes_numeric_layer_notation() -> None:
    front = NetClass(
        name="front",
        width=0.2,
        clearance=0.2,
        layer=1,
    )
    back = NetClass(
        name="back",
        width=0.2,
        clearance=0.2,
        layer="2",
    )

    assert front.layer == "F.Cu"
    assert back.layer == "B.Cu"


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        ("Use a six-layer PCB.", 6),
        ("采用八层板。", 8),
        ("Do not use a two-layer board; use a six-layer PCB.", 6),
    ],
)
def test_route_plan_honors_generic_layer_counts(
    requirement: str,
    expected: int,
) -> None:
    state = PipelineState(requirement_text=requirement)

    RoutePlanStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    plan = state.artifact(PipelineStep.ROUTE_PLAN)
    assert isinstance(plan, RoutePlan)
    assert plan.layers == expected


def test_route_plan_checks_via_drill_and_annular_ring() -> None:
    plan = RoutePlan(
        layers=2,
        net_classes=[
            NetClass(
                name="unsafe",
                width=0.2,
                clearance=0.2,
                via_diameter=0.6,
                via_drill=0.4,
                layer="F.Cu",
            ),
        ],
    )

    checks = RoutePlanStep().check(PipelineState(requirement_text="x"), plan)

    assert not next(check for check in checks if check.name == "annular_ring_ok").ok


def test_route_plan_thin_track_blocks() -> None:
    bad = json.dumps({
        "layers": 2,
        "net_classes": [
            {"name": "signal", "width": 0.05, "clearance": 0.2,
             "via_diameter": 0.6, "via_drill": 0.3, "layer": "F.Cu"}
        ],
    })
    state = PipelineState(requirement_text="x")
    RoutePlanStep().run(state, PipelineContext(mode=LlmMode.AUTO, client=FakeLLM([bad])))
    result = state.results[-1]
    assert result.blocked
    assert any(c.name == "track_width_ok" and not c.ok for c in result.checks)


def test_planes_ground_plane_present() -> None:
    state = PipelineState(requirement_text="x")
    state.artifacts[PipelineStep.SCH_CONNECTIONS] = NetlistIntent(
        nets=[NetIntent(name="GND", kind="ground",
                        pins=[LogicalPin(ref="U1", pin="1"), LogicalPin(ref="U1", pin="2")])],
        supply_nets=[], ground_net="GND")
    RoutePlanesStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    plan = state.artifact(PipelineStep.ROUTE_PLANES)
    assert isinstance(plan, PlanePlan)
    result = state.results[-1]
    assert not result.blocked
    assert any("GND" in p for p in plan.planes)


def test_planes_missing_ground_blocks() -> None:
    bad = json.dumps({"ground_net": "GND", "planes": [], "critical_nets": []})
    state = PipelineState(requirement_text="x")
    RoutePlanesStep().run(state, PipelineContext(mode=LlmMode.AUTO, client=FakeLLM([bad])))
    result = state.results[-1]
    assert result.blocked
    assert any(c.name == "ground_plane_present" and not c.ok for c in result.checks)


def _two_layer_stm32_state() -> PipelineState:
    """A 2-layer board whose netlist has nothing in common with an ESP32."""
    state = PipelineState(requirement_text="two-layer STM32F103C8T6 board")
    state.artifacts[PipelineStep.SCH_CONNECTIONS] = NetlistIntent(
        nets=[
            NetIntent(name="GND", kind="ground", pins=[
                LogicalPin(ref="U1", pin="23"), LogicalPin(ref="U1", pin="35")]),
            NetIntent(name="VDD33", kind="power", pins=[
                LogicalPin(ref="U1", pin="24"), LogicalPin(ref="U1", pin="36")]),
            NetIntent(name="HSE_OSC_IN", kind="clock", pins=[
                LogicalPin(ref="U1", pin="5"), LogicalPin(ref="X1", pin="1")]),
        ],
        supply_nets=["VDD33"], ground_net="GND")
    state.artifacts[PipelineStep.ROUTE_PLAN] = RoutePlan(
        layers=2,
        net_classes=[NetClass(name="default", width=0.25, clearance=0.2)],
    )
    return state


def test_planes_propose_receives_real_nets_and_layer_count() -> None:
    """The prompt must carry the netlist and stackup, not just the ground net.

    Starved of both, this step restated whichever reference board the retrieved
    knowledge happened to describe.
    """
    seen: list[str] = []

    class RecordingLLM:
        def complete(self, system, user, temperature=0.2):
            seen.append(user)
            return json.dumps({
                "ground_net": "GND",
                "planes": ["B.Cu:GND"],
                "critical_nets": ["VDD33", "HSE_OSC_IN"],
            })

    state = _two_layer_stm32_state()
    RoutePlanesStep().run(state, PipelineContext(mode=LlmMode.AUTO, client=RecordingLLM()))

    assert seen, "the step did not call the model"
    prompt = seen[0]
    assert "HSE_OSC_IN" in prompt
    assert "VDD33" in prompt
    assert "Copper layers: 2" in prompt
    assert "B.Cu" in prompt
    assert "In1.Cu" not in prompt
    assert not state.results[-1].blocked


def test_planes_reject_critical_nets_absent_from_netlist() -> None:
    """Guards the ESP32 net names that leaked into a 2-layer STM32 board."""
    leaked = json.dumps({
        "ground_net": "GND",
        "planes": ["B.Cu:GND"],
        "critical_nets": ["RF_TX", "ANTENNA", "HSPI_CLK", "VDD_SDIO", "VDD33"],
    })
    state = _two_layer_stm32_state()
    RoutePlanesStep().run(state, PipelineContext(mode=LlmMode.AUTO, client=FakeLLM([leaked])))

    result = state.results[-1]
    assert result.blocked
    failed = next(c for c in result.checks if c.name == "critical_nets_exist")
    assert not failed.ok
    for absent in ("RF_TX", "ANTENNA", "HSPI_CLK", "VDD_SDIO"):
        assert absent in failed.message
    assert "VDD33" not in failed.message


def test_planes_reject_layers_outside_the_stackup() -> None:
    """A 2-layer board has no L3; such a plane is a leak, not a placement."""
    leaked = json.dumps({
        "ground_net": "GND",
        "planes": ["L1:Signal (TOP)", "L2:GND", "L3:POWER (VDD_SDIO)", "L4:Signal"],
        "critical_nets": [],
    })
    state = _two_layer_stm32_state()
    RoutePlanesStep().run(state, PipelineContext(mode=LlmMode.AUTO, client=FakeLLM([leaked])))

    result = state.results[-1]
    assert result.blocked
    failed = next(c for c in result.checks if c.name == "plane_layers_in_stackup")
    assert not failed.ok
    # 'L2:GND' still satisfies the ground-plane check, which is why the old
    # single check passed this artifact.
    assert any(c.name == "ground_plane_present" and c.ok for c in result.checks)


def test_planes_accept_inner_layers_on_a_four_layer_stackup() -> None:
    state = _two_layer_stm32_state()
    state.artifacts[PipelineStep.ROUTE_PLAN] = RoutePlan(
        layers=4,
        net_classes=[NetClass(name="default", width=0.25, clearance=0.2)],
    )
    ok = json.dumps({
        "ground_net": "GND",
        "planes": ["In1.Cu:GND", "In2.Cu:VDD33"],
        "critical_nets": ["HSE_OSC_IN"],
    })
    RoutePlanesStep().run(state, PipelineContext(mode=LlmMode.AUTO, client=FakeLLM([ok])))

    result = state.results[-1]
    assert not result.blocked
    assert all(c.ok for c in result.checks)


def test_copper_layer_tokens_track_the_stackup() -> None:
    assert _copper_layer_tokens(2) == {"F.Cu", "B.Cu"}
    assert _copper_layer_tokens(4) == {"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"}
    assert _copper_layer_tokens(1) == {"F.Cu"}


def _pcb_with_tracks(tmp_path, *widths: tuple[float, str]) -> str:
    """Write a minimal .kicad_pcb carrying the given copper segments."""
    segments = "\n".join(
        f'  (segment (start 0 0) (end 1 {index}) (width {width})'
        f' (layer "{layer}") (net 1))'
        for index, (width, layer) in enumerate(widths)
    )
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(
        f'(kicad_pcb (version 20240108) (generator "test")\n{segments}\n)\n',
        encoding="utf-8",
    )
    return str(pcb)


def _routed_state(tmp_path, *, widths, planned, tracks=None) -> PipelineState:
    state = PipelineState(requirement_text="two-layer board")
    state.artifacts[PipelineStep.ROUTE_PLAN] = RoutePlan(
        layers=2,
        net_classes=[
            NetClass(name=name, width=width, clearance=0.2)
            for name, width in planned
        ],
    )
    state.artifacts[PipelineStep.ROUTE_SIGNALS] = RouteResult(
        method="freerouting",
        required=True,
        routed_nets=1,
        total_nets=1,
        routed_tracks=len(widths) if tracks is None else tracks,
        unconnected=0,
    )
    state.artifacts[PipelineStep.LAYOUT_WRITE] = PcbWriteResult(
        pcb_path=_pcb_with_tracks(tmp_path, *widths),
        component_count=2,
        has_board_outline=True,
    )
    return state


def test_route_fab_flags_plan_collapsed_to_narrowest_class(tmp_path) -> None:
    """The router takes one global width, so a differentiated plan collapses.

    Every track shipped at the narrowest planned class (0.15 mm) while the plan
    specified up to 0.5 mm for power and ground — a current-capacity defect that
    the plan-only audit reported as zero violations.
    """
    state = _routed_state(
        tmp_path,
        widths=[(0.15, "F.Cu"), (0.15, "B.Cu")],
        planned=[("Signal", 0.15), ("Power_5V", 0.5), ("Ground", 0.5)],
    )
    RouteFabStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    result = state.results[-1]
    assert result.blocked
    audit = state.artifact(PipelineStep.ROUTE_FAB)
    assert isinstance(audit, FabAudit)
    joined = " ".join(audit.violations)
    assert "0.15" in joined
    assert "0.5" in joined
    assert "Power_5V" in joined
    assert "Ground" in joined


def test_route_fab_accepts_board_that_honours_the_plan(tmp_path) -> None:
    state = _routed_state(
        tmp_path,
        widths=[(0.15, "F.Cu"), (0.5, "F.Cu"), (0.5, "B.Cu")],
        planned=[("Signal", 0.15), ("Power_5V", 0.5), ("Ground", 0.5)],
    )
    RouteFabStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    result = state.results[-1]
    assert not result.blocked
    assert all(c.ok for c in result.checks)


def test_route_fab_flags_tracks_below_the_fab_minimum(tmp_path) -> None:
    state = _routed_state(
        tmp_path,
        widths=[(0.05, "F.Cu")],
        planned=[("Signal", 0.05)],
    )
    RouteFabStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    audit = state.artifact(PipelineStep.ROUTE_FAB)
    assert isinstance(audit, FabAudit)
    assert any("fab minimum" in v for v in audit.violations)
    assert state.results[-1].blocked


def test_route_fab_refuses_hollow_pass_when_nothing_was_routed(tmp_path) -> None:
    """Zero tracks means zero width violations, which is not a clean board."""
    state = _routed_state(
        tmp_path,
        widths=[],
        planned=[("Signal", 0.15)],
        tracks=0,
    )
    RouteFabStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    result = state.results[-1]
    assert result.blocked
    audit = state.artifact(PipelineStep.ROUTE_FAB)
    assert isinstance(audit, FabAudit)
    assert any("no tracks" in v for v in audit.violations)


def test_route_fab_degrades_without_a_written_board() -> None:
    """Partial state must not crash the audit; it falls back to plan-only."""
    state = PipelineState(requirement_text="x")
    state.artifacts[PipelineStep.ROUTE_PLAN] = RoutePlan(
        layers=2,
        net_classes=[NetClass(name="signal", width=0.25, clearance=0.2)],
    )
    state.artifacts[PipelineStep.ROUTE_SIGNALS] = RouteResult(
        method="freerouting", required=True, routed_tracks=12,
    )
    RouteFabStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    assert not state.results[-1].blocked


def test_copper_track_widths_ignores_vias_and_non_copper(tmp_path) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20240108) (generator "test")\n'
        '  (segment (start 0 0) (end 1 0) (width 0.3) (layer "F.Cu") (net 1))\n'
        '  (segment (start 0 1) (end 1 1) (width 0.4) (layer "B.Cu") (net 2))\n'
        '  (via (at 2 2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n'
        '  (gr_line (start 0 0) (end 5 0) (width 0.05) (layer "Edge.Cuts"))\n'
        ')\n',
        encoding="utf-8",
    )
    widths = routing.copper_track_widths(str(pcb))

    assert sorted(widths) == [(0.3, "F.Cu"), (0.4, "B.Cu")]


def test_copper_track_widths_tolerates_a_missing_file(tmp_path) -> None:
    assert routing.copper_track_widths(str(tmp_path / "absent.kicad_pcb")) == []



def test_signal_routing_degrades_gracefully(monkeypatch) -> None:
    monkeypatch.delenv("FREEROUTING_JAR", raising=False)
    state = PipelineState(requirement_text="x")
    state.artifacts[PipelineStep.SCH_PINMAP] = PinMapPlan(nets=[
        MappedNet(name="N1", kind="signal", pins=[
            MappedPin(ref="R1", logical="1", number="1"),
            MappedPin(ref="R2", logical="1", number="1")])])
    RouteSignalsStep().run(
        state,
        PipelineContext(mode=LlmMode.OFFLINE, require_freerouting=False),
    )
    result = state.results[-1]
    art = state.artifact(PipelineStep.ROUTE_SIGNALS)
    assert isinstance(art, RouteResult)
    # Routing deferred is a warning, never a hard block.
    assert not result.blocked
    assert art.method in ("deferred", "freerouting_available")


def test_signal_routing_required_blocks_when_not_completed() -> None:
    state = PipelineState(requirement_text="x")
    RouteSignalsStep().run(
        state,
        PipelineContext(mode=LlmMode.OFFLINE, require_freerouting=True),
    )
    result = state.results[-1]
    art = state.artifact(PipelineStep.ROUTE_SIGNALS)
    assert isinstance(art, RouteResult)
    assert art.required
    assert result.blocked
    assert any(c.name == "signals_routed" for c in result.error_checks)


def test_layer_escalation_restarts_from_unrouted_board(tmp_path, monkeypatch) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    baseline = tmp_path / "board.unrouted.kicad_pcb"
    pcb.write_bytes(b"stale partial routes")
    baseline.write_bytes(b"clean board")
    dsn = tmp_path / "result.dsn"
    ses = tmp_path / "result.ses"
    calls: list[tuple[int, int]] = []

    def fake_autoroute(path, netmap, *, max_passes, layer_count, **route_rules):
        calls.append((layer_count, max_passes))
        assert path.read_bytes() == b"clean board"
        assert route_rules["clearance_mm"] == 0.2
        assert route_rules["track_width_mm"] == 0.2
        if layer_count == 2:
            path.write_bytes(b"partial two-layer routes")
            return RouteOutcome(
                "freerouting",
                True,
                2,
                len(netmap),
                2,
                10,
                1,
                "incomplete",
                str(dsn),
                str(ses),
            )
        return RouteOutcome(
            "freerouting",
            True,
            4,
            len(netmap),
            2,
            20,
            0,
            "complete",
            str(dsn),
            str(ses),
        )

    monkeypatch.setattr(routing, "autoroute", fake_autoroute)
    state = PipelineState(requirement_text="generic board", project_name="board")
    state.artifacts[PipelineStep.SCH_PINMAP] = PinMapPlan(nets=[
        MappedNet(
            name="SIG",
            pins=[
                MappedPin(ref="R1", logical="1", number="1"),
                MappedPin(ref="R2", logical="1", number="1"),
            ],
        )
    ])
    state.artifacts[PipelineStep.ROUTE_PLAN] = RoutePlan(
        layers=2,
        net_classes=[
            NetClass(
                name="signal",
                width=0.2,
                clearance=0.2,
                via_diameter=0.6,
                via_drill=0.3,
                layer="F.Cu",
            )
        ],
    )

    RouteSignalsStep().run(
        state,
        PipelineContext(
            mode=LlmMode.OFFLINE,
            out_dir=str(tmp_path),
            require_freerouting=True,
        ),
    )

    assert calls == [(2, 20), (4, 26)]
    assert not state.results[-1].blocked


def test_explicit_layer_count_does_not_escalate(tmp_path, monkeypatch) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    baseline = tmp_path / "board.unrouted.kicad_pcb"
    pcb.write_bytes(b"stale partial routes")
    baseline.write_bytes(b"clean board")
    calls: list[int] = []

    def fake_autoroute(path, netmap, *, max_passes, layer_count, **_rules):
        calls.append(layer_count)
        return RouteOutcome(
            "freerouting",
            True,
            layer_count,
            len(netmap),
            2,
            10,
            1,
            "incomplete",
            str(tmp_path / "result.dsn"),
            str(tmp_path / "result.ses"),
        )

    monkeypatch.setattr(routing, "autoroute", fake_autoroute)
    state = PipelineState(
        requirement_text="Use a two-layer PCB with 0.2 mm trace width",
        project_name="board",
    )
    state.artifacts[PipelineStep.SCH_PINMAP] = PinMapPlan(nets=[
        MappedNet(
            name="SIG",
            pins=[
                MappedPin(ref="R1", logical="1", number="1"),
                MappedPin(ref="R2", logical="1", number="1"),
            ],
        )
    ])
    state.artifacts[PipelineStep.ROUTE_PLAN] = RoutePlan(
        layers=2,
        net_classes=[
            NetClass(
                name="signal",
                width=0.2,
                clearance=0.2,
                via_diameter=0.6,
                via_drill=0.3,
                layer="F.Cu",
            )
        ],
    )

    RouteSignalsStep().run(
        state,
        PipelineContext(
            mode=LlmMode.OFFLINE,
            out_dir=str(tmp_path),
            require_freerouting=True,
        ),
    )

    assert calls == [2, 2]
    assert state.results[-1].blocked
    artifact = state.artifact(PipelineStep.ROUTE_SIGNALS)
    assert isinstance(artifact, RouteResult)
    assert artifact.layers == 2


def test_fab_min_geometry_retry_is_used_when_dimensions_are_not_fixed(
    tmp_path,
    monkeypatch,
) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    baseline = tmp_path / "board.unrouted.kicad_pcb"
    pcb.write_bytes(b"stale partial routes")
    baseline.write_bytes(b"clean board")
    calls: list[float] = []

    def fake_autoroute(path, netmap, *, track_width_mm, layer_count, **_rules):
        calls.append(track_width_mm)
        complete = track_width_mm < 0.2
        return RouteOutcome(
            "freerouting",
            True,
            layer_count,
            len(netmap),
            2,
            20 if complete else 10,
            0 if complete else 1,
            "complete" if complete else "incomplete",
            str(tmp_path / "result.dsn"),
            str(tmp_path / "result.ses"),
        )

    monkeypatch.setattr(routing, "autoroute", fake_autoroute)
    state = PipelineState(
        requirement_text="Use a two-layer PCB",
        project_name="board",
    )
    state.artifacts[PipelineStep.SCH_PINMAP] = PinMapPlan(nets=[
        MappedNet(
            name="SIG",
            pins=[
                MappedPin(ref="R1", logical="1", number="1"),
                MappedPin(ref="R2", logical="1", number="1"),
            ],
        )
    ])
    state.artifacts[PipelineStep.ROUTE_PLAN] = RoutePlan(
        layers=2,
        net_classes=[
            NetClass(
                name="signal",
                width=0.2,
                clearance=0.2,
                via_diameter=0.6,
                via_drill=0.3,
                layer="F.Cu",
            )
        ],
    )

    RouteSignalsStep().run(
        state,
        PipelineContext(
            mode=LlmMode.OFFLINE,
            out_dir=str(tmp_path),
            require_freerouting=True,
        ),
    )

    assert calls == [0.2, config.process_capability().min_track_width]
    result = state.results[-1]
    artifact = state.artifact(PipelineStep.ROUTE_SIGNALS)
    assert not result.blocked
    assert isinstance(artifact, RouteResult)
    assert artifact.unconnected == 0
    assert artifact.note.startswith("adaptive fab-min routing rules used")


def test_fab_audit_blocks_on_violation() -> None:
    state = PipelineState(requirement_text="x")
    state.artifacts[PipelineStep.ROUTE_PLAN] = RoutePlan(
        layers=2,
        net_classes=[NetClass(name="signal", width=0.02, clearance=0.02,
                              via_diameter=0.1, via_drill=0.05, layer="F.Cu")],
    )
    RouteFabStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    result = state.results[-1]
    assert result.blocked
    assert any(c.name == "fab_rules_met" and not c.ok for c in result.checks)


def test_fab_audit_passes_clean_plan() -> None:
    state = PipelineState(requirement_text="x")
    RoutePlanStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    RouteFabStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    result = state.results[-1]
    art = state.artifact(PipelineStep.ROUTE_FAB)
    assert isinstance(art, FabAudit)
    assert not result.blocked and not art.violations
