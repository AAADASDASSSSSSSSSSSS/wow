"""Task 6: schematic connection design (netlist intent) + bottom-line check."""

from __future__ import annotations

import json

import pytest

from ratsnestpro.agents.llm import LlmMode
from ratsnestpro.orchestration.pipeline import (
    CheckResult,
    PipelineContext,
    PipelineState,
    PipelineStep,
    SchConnectionsStep,
    StepResult,
    _apply_netlist_patch,
    _complete_evident_connector_power_pins,
    _connection_repair_scope,
    _functional_connection_checks,
    _limit_netlist_patch_to_scope,
    _normalize_additional_parts,
    _normalize_standard_connector_no_connects,
    _remove_invalid_no_connect_pins,
    _remove_unknown_netlist_refs,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    LogicalPin,
    NetIntent,
    NetlistIntent,
    NetlistPatch,
    SelectedPart,
    SelectionPlan,
)


class FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        self.prompts.append(user)
        return self._responses.pop(0) if self._responses else "{}"


def test_repair_normalization_removes_only_unselected_refs() -> None:
    selection = SelectionPlan(parts=[
        SelectedPart(ref="U1", symbol="Device:R", value="MCU", role="mcu"),
    ])
    intent = NetlistIntent(
        nets=[
            NetIntent(
                name="GND",
                kind="ground",
                pins=[
                    LogicalPin(ref="U1", pin="1"),
                    LogicalPin(ref="R99", pin="2"),
                ],
            ),
        ],
        no_connect_pins=[
            LogicalPin(ref="U1", pin="2"),
            LogicalPin(ref="J99", pin="1"),
        ],
        ground_net="GND",
    )

    normalized = _remove_unknown_netlist_refs(intent, selection)

    assert [pin.ref for pin in normalized.nets[0].pins] == ["U1"]
    assert [pin.ref for pin in normalized.no_connect_pins] == ["U1"]


def test_net_intent_deduplicates_identical_pin_mentions() -> None:
    net = NetIntent.model_validate({
        "name": "3V3",
        "pins": [
            {"ref": "U1", "pin": "1"},
            {"ref": "U1", "pin": "1"},
            {"ref": "C1", "pin": "1"},
        ],
    })

    assert [pin.key() for pin in net.pins] == ["U1:1", "C1:1"]


def test_connection_repair_patch_cannot_rewrite_unrelated_nets() -> None:
    selection = SelectionPlan(parts=[
        SelectedPart(ref="U2", symbol="Device:R", value="Buck", role="buck_regulator"),
        SelectedPart(ref="C1", symbol="Device:C", value="Cap", role="buck_bootstrap_capacitor"),
        SelectedPart(ref="U1", symbol="Device:R", value="MCU", role="mcu"),
    ])
    plan = NetlistIntent(
        nets=[
            NetIntent(
                name="BUCK_BOOT",
                pins=[LogicalPin(ref="U2", pin="1")],
            ),
            NetIntent(
                name="GOOD",
                pins=[
                    LogicalPin(ref="U1", pin="1"),
                    LogicalPin(ref="U1", pin="2"),
                ],
            ),
            NetIntent(
                name="GND",
                kind="ground",
                pins=[
                    LogicalPin(ref="U2", pin="2"),
                    LogicalPin(ref="C1", pin="2"),
                ],
            ),
        ],
        ground_net="GND",
    )
    checks = [
        CheckResult(
            name="no_single_pin_nets",
            ok=False,
            message="single-pin/empty nets: ['BUCK_BOOT']",
        ),
    ]
    related_refs, relevant_nets, _ = _connection_repair_scope(
        selection,
        plan,
        checks,
    )
    patch = NetlistPatch(
        upsert_nets=[
            NetIntent(
                name="BUCK_BOOT",
                pins=[
                    LogicalPin(ref="U2", pin="1"),
                    LogicalPin(ref="C1", pin="1"),
                ],
            ),
            NetIntent(
                name="GOOD",
                pins=[LogicalPin(ref="U1", pin="1")],
            ),
        ],
        add_no_connect_pins=[
            LogicalPin(ref="U1", pin="2"),
        ],
    )

    limited = _limit_netlist_patch_to_scope(
        patch,
        plan,
        related_refs,
        relevant_nets,
    )

    assert [net.name for net in limited.upsert_nets] == ["BUCK_BOOT"]
    assert not limited.add_no_connect_pins


def test_led_repair_scope_includes_controller_for_unused_gpio() -> None:
    selection = SelectionPlan(parts=[
        SelectedPart(ref="U1", symbol="Test:MCU", value="MCU", role="mcu"),
        SelectedPart(
            ref="LED1",
            symbol="Device:LED",
            value="LED",
            role="system_status_led",
        ),
        SelectedPart(
            ref="R1",
            symbol="Device:R",
            value="470",
            role="system_led_current_limit",
        ),
    ])
    plan = NetlistIntent(
        nets=[
            NetIntent(
                name="LED_A",
                pins=[
                    LogicalPin(ref="LED1", pin="1"),
                    LogicalPin(ref="R1", pin="1"),
                ],
            ),
            NetIntent(
                name="LED_B",
                pins=[
                    LogicalPin(ref="LED1", pin="2"),
                    LogicalPin(ref="R1", pin="2"),
                ],
            ),
            NetIntent(
                name="MCU_GOOD",
                pins=[
                    LogicalPin(ref="U1", pin="1"),
                    LogicalPin(ref="U1", pin="2"),
                ],
            ),
        ],
    )
    checks = [
        CheckResult(
            name="led_current_limit_in_series:LED1",
            ok=False,
            message="LED1 and R1 are wired in parallel",
        )
    ]

    related_refs, relevant_nets, _ = _connection_repair_scope(
        selection,
        plan,
        checks,
    )

    assert {"U1", "LED1", "R1"} <= related_refs
    assert "MCU_GOOD" not in relevant_nets


def test_repair_scope_expands_unfamiliar_semantic_role_family() -> None:
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="U8",
            symbol="Interface_Ethernet:LAN8720A",
            value="LAN8720A",
            footprint="Package_QFP:LQFP-48_7x7mm_P0.5mm",
            role="ethernet_phy",
        ),
        SelectedPart(
            ref="R8",
            symbol="Device:R",
            value="12k1",
            footprint="Resistor_SMD:R_0603_1608Metric",
            role="ethernet_phy_bias",
        ),
        SelectedPart(
            ref="C8",
            symbol="Device:C",
            value="100n",
            footprint="Capacitor_SMD:C_0603_1608Metric",
            role="ethernet_phy_decoupling",
        ),
        SelectedPart(
            ref="U1",
            symbol="MCU_ST_STM32F4:STM32F405RGTx",
            value="STM32F405RGT6",
            footprint="Package_QFP:LQFP-64_10x10mm_P0.5mm",
            role="mcu",
        ),
    ])
    plan = NetlistIntent(nets=[
        NetIntent(
            name="ETH_MDIO",
            pins=[LogicalPin(ref="U8", pin="1")],
        ),
    ])
    checks = [
        CheckResult(
            name="no_single_pin_nets",
            ok=False,
            message="single-pin net ETH_MDIO on U8",
        ),
    ]

    related_refs, relevant_nets, _ = _connection_repair_scope(
        selection,
        plan,
        checks,
    )

    assert {"U8", "R8", "C8"} <= related_refs
    assert "ETH_MDIO" in relevant_nets


def test_repair_scope_includes_interface_siblings_and_failed_connector() -> None:
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="Test:MCU",
            value="MCU",
            role="mcu",
        ),
        SelectedPart(
            ref="U2",
            symbol="Test:Peripheral",
            value="Peripheral",
            role="serial_memory",
        ),
        SelectedPart(
            ref="J2",
            symbol="Connector_Generic:Conn_02x05_Odd_Even",
            value="Debug",
            role="standard_debug_header",
        ),
    ])
    plan = NetlistIntent(
        nets=[
            NetIntent(
                name="SER_IO0",
                pins=[LogicalPin(ref="U1", pin="IO0")],
            ),
            NetIntent(
                name="SER_IO1",
                pins=[
                    LogicalPin(ref="U1", pin="IO1"),
                    LogicalPin(ref="U2", pin="DO"),
                ],
            ),
            NetIntent(
                name="DEBUG_DATA",
                pins=[
                    LogicalPin(ref="U1", pin="DBG"),
                    LogicalPin(ref="J2", pin="2"),
                ],
            ),
            NetIntent(
                name="DEBUG_CLOCK",
                pins=[
                    LogicalPin(ref="U1", pin="CLK"),
                    LogicalPin(ref="J2", pin="4"),
                ],
            ),
            NetIntent(
                name="3V3",
                kind="power",
                pins=[
                    LogicalPin(ref="U1", pin="VDD"),
                    LogicalPin(ref="J2", pin="1"),
                ],
            ),
            NetIntent(
                name="GND",
                kind="ground",
                pins=[
                    LogicalPin(ref="U1", pin="GND"),
                    LogicalPin(ref="J2", pin="3"),
                ],
            ),
        ],
        supply_nets=["3V3"],
        ground_net="GND",
    )
    checks = [
        CheckResult(
            name="no_single_pin_nets",
            ok=False,
            message="single-pin/empty nets: ['SER_IO0']",
        ),
        CheckResult(
            name="standard_mapping:J2",
            ok=False,
            message="J2 has an invalid standard interface mapping",
        ),
    ]

    related_refs, relevant_nets, _ = _connection_repair_scope(
        selection,
        plan,
        checks,
    )

    assert {"U1", "U2", "J2"} <= related_refs
    assert {
        "SER_IO0",
        "SER_IO1",
        "DEBUG_DATA",
        "DEBUG_CLOCK",
        "3V3",
        "GND",
    } <= relevant_nets


def test_invalid_advisory_no_connect_pin_is_removed(monkeypatch) -> None:
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="J1",
            symbol="Connector:USB_C_Receptacle_USB2.0_14P",
            value="USB-C",
            footprint="Connector_USB:USB_C_Receptacle",
            role="usb_connector",
        ),
    ])
    plan = NetlistIntent(
        no_connect_pins=[
            LogicalPin(ref="J1", pin="A8"),
            LogicalPin(ref="J1", pin="S1"),
        ],
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda _symbol: [{"number": "S1", "name": "Shield"}],
    )

    normalized = _remove_invalid_no_connect_pins(selection, plan)

    assert [pin.pin for pin in normalized.no_connect_pins] == ["S1"]


def test_additional_parts_are_case_insensitive_upserts() -> None:
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="R1",
            symbol="Device:R",
            value="1k",
            footprint="Resistor:R",
            role="existing",
        ),
    ])
    plan = NetlistIntent(additional_parts=[
        SelectedPart(
            ref="r1",
            symbol="Device:R",
            value="2k",
            footprint="Resistor:R",
            role="duplicate_existing",
        ),
        SelectedPart(
            ref="C1",
            symbol="Device:C",
            value="1u",
            footprint="Capacitor:C",
            role="first",
        ),
        SelectedPart(
            ref="c1",
            symbol="Device:C",
            value="2u",
            footprint="Capacitor:C",
            role="replacement",
        ),
    ])

    normalized = _normalize_additional_parts(selection, plan)

    assert [(part.ref, part.value) for part in normalized.additional_parts] == [
        ("c1", "2u")
    ]


def test_standard_swd_reserved_pins_are_normalized_to_no_connect(
    monkeypatch,
) -> None:
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="J2",
            symbol="Test:Header10",
            value="SWD",
            footprint="Connector:Header10",
            role="cortex_swd_10_pin",
        ),
    ])
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda _symbol: [
            {"number": str(number), "name": str(number)}
            for number in range(1, 11)
        ],
    )
    plan = NetlistIntent(
        nets=[
            NetIntent(
                name="3V3",
                kind="power",
                pins=[
                    LogicalPin(ref="U1", pin="VDD"),
                    LogicalPin(ref="J2", pin="1"),
                    LogicalPin(ref="J2", pin="6"),
                    LogicalPin(ref="J2", pin="7"),
                    LogicalPin(ref="J2", pin="8"),
                ],
            ),
            NetIntent(
                name="SWO",
                pins=[
                    LogicalPin(ref="U1", pin="SWO"),
                    LogicalPin(ref="J2", pin="6"),
                ],
            ),
        ],
        supply_nets=["3V3"],
    )

    normalized = _normalize_standard_connector_no_connects(selection, plan)

    supply = next(net for net in normalized.nets if net.name == "3V3")
    swo = next(net for net in normalized.nets if net.name == "SWO")
    assert [pin.pin for pin in supply.pins if pin.ref == "J2"] == ["1"]
    assert [pin.pin for pin in swo.pins if pin.ref == "J2"] == ["6"]
    assert {(pin.ref, pin.pin) for pin in normalized.no_connect_pins} == {
        ("J2", "7"),
        ("J2", "8"),
    }


def test_supply_pin_failure_includes_existing_power_rails() -> None:
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="U2",
            symbol="MCU_Test:Controller",
            value="Controller",
            footprint="Package_QFP:LQFP-48_7x7mm_P0.5mm",
            role="mcu",
        ),
    ])
    plan = NetlistIntent(
        nets=[
            NetIntent(
                name="3V3",
                kind="power",
                pins=[
                    LogicalPin(ref="U2", pin="1"),
                    LogicalPin(ref="U2", pin="2"),
                ],
            ),
            NetIntent(
                name="GND",
                kind="ground",
                pins=[
                    LogicalPin(ref="U2", pin="3"),
                    LogicalPin(ref="U2", pin="4"),
                ],
            ),
        ],
        supply_nets=["3V3"],
        ground_net="GND",
    )
    checks = [
        CheckResult(
            name="component_pins_accounted",
            ok=False,
            message="unaccounted real pins: ['U2:10(IOVDD)']",
        ),
    ]

    _, relevant_nets, _ = _connection_repair_scope(selection, plan, checks)

    assert "3V3" in relevant_nets


def test_missing_connector_pin_repair_includes_supply_and_ground(
    monkeypatch,
) -> None:
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="J7",
            symbol="Test:Header4",
            value="Expansion",
            footprint="Connector:Header4",
            role="sensor_bus_header",
        ),
    ])
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda _symbol: [
            {"number": str(number), "name": f"PIN_{number}"}
            for number in range(1, 5)
        ],
    )
    plan = NetlistIntent(
        nets=[
            NetIntent(
                name="3V3",
                kind="power",
                pins=[
                    LogicalPin(ref="U1", pin="VDD"),
                    LogicalPin(ref="C1", pin="1"),
                ],
            ),
            NetIntent(
                name="GND",
                kind="ground",
                pins=[
                    LogicalPin(ref="U1", pin="GND"),
                    LogicalPin(ref="J7", pin="4"),
                ],
            ),
            NetIntent(
                name="BUS_DATA",
                pins=[
                    LogicalPin(ref="U1", pin="DATA"),
                    LogicalPin(ref="J7", pin="1"),
                ],
            ),
        ],
        supply_nets=["3V3"],
        ground_net="GND",
    )
    checks = [
        CheckResult(
            name="component_pins_accounted",
            ok=False,
            message="real pins not accounted: ['J7:3(PIN_3)']",
        ),
    ]

    _, relevant_nets, _ = _connection_repair_scope(selection, plan, checks)

    assert {"3V3", "GND", "BUS_DATA"} <= relevant_nets


def test_connector_power_pin_is_completed_from_two_independent_rail_votes(
    monkeypatch,
) -> None:
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="J7",
            symbol="Test:Header4",
            value="Expansion",
            footprint="Connector:Header4",
            role="sensor_bus_header",
        ),
        SelectedPart(
            ref="R1",
            symbol="Test:R",
            value="4k7",
            footprint="Resistor:R",
            role="data_bias",
        ),
        SelectedPart(
            ref="R2",
            symbol="Test:R",
            value="4k7",
            footprint="Resistor:R",
            role="clock_bias",
        ),
        SelectedPart(
            ref="U1",
            symbol="Test:LargeIC",
            value="Controller",
            footprint="Package:IC",
            role="controller",
        ),
    ])
    pin_map = {
        "Test:Header4": [
            {"number": str(number), "name": f"PIN_{number}"}
            for number in range(1, 5)
        ],
        "Test:R": [
            {"number": "1", "name": "1"},
            {"number": "2", "name": "2"},
        ],
        "Test:LargeIC": [
            {"number": str(number), "name": f"P{number}"}
            for number in range(1, 33)
        ],
    }
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda symbol: pin_map[symbol],
    )
    plan = NetlistIntent(
        nets=[
            NetIntent(
                name="3V3",
                kind="power",
                pins=[
                    LogicalPin(ref="R1", pin="1"),
                    LogicalPin(ref="R2", pin="1"),
                ],
            ),
            NetIntent(
                name="GND",
                kind="ground",
                pins=[
                    LogicalPin(ref="U1", pin="1"),
                    LogicalPin(ref="J7", pin="4"),
                ],
            ),
            NetIntent(
                name="BUS_DATA",
                pins=[
                    LogicalPin(ref="U1", pin="2"),
                    LogicalPin(ref="R1", pin="2"),
                    LogicalPin(ref="J7", pin="1"),
                ],
            ),
            NetIntent(
                name="BUS_CLOCK",
                pins=[
                    LogicalPin(ref="U1", pin="3"),
                    LogicalPin(ref="R2", pin="2"),
                    LogicalPin(ref="J7", pin="2"),
                ],
            ),
        ],
        supply_nets=["3V3"],
        ground_net="GND",
    )

    completed = _complete_evident_connector_power_pins(selection, plan)
    supply = next(net for net in completed.nets if net.name == "3V3")

    assert ("J7", "3") in {(pin.ref, pin.pin) for pin in supply.pins}


def test_multiple_power_outputs_on_one_net_are_rejected(monkeypatch) -> None:
    state = PipelineState(requirement_text="x")
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="Regulator:External",
            value="LDO",
            footprint="Package:U1",
            role="regulator",
        ),
        SelectedPart(
            ref="U2",
            symbol="MCU:Controller",
            value="MCU",
            footprint="Package:U2",
            role="mcu",
        ),
    ])
    plan = NetlistIntent(
        nets=[
            NetIntent(
                name="3V3",
                kind="power",
                pins=[
                    LogicalPin(ref="U1", pin="OUT"),
                    LogicalPin(ref="U2", pin="VREG_OUT"),
                ],
            ),
        ],
        supply_nets=["3V3"],
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.config.symbol_dir",
        lambda: "/symbols",
    )

    def fake_pins(symbol):
        name = "OUT" if symbol == "Regulator:External" else "VREG_OUT"
        return [{"number": "1", "name": name, "type": "power_out"}]

    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        fake_pins,
    )

    checks = SchConnectionsStep().check(state, plan)

    assert not next(
        check for check in checks if check.name == "single_power_output_per_net"
    ).ok


def test_power_input_only_signal_net_is_rejected(monkeypatch) -> None:
    state = PipelineState(requirement_text="generic controller")
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="Test:Controller",
            value="Controller",
            footprint="Package:U1",
            role="mcu",
        ),
        SelectedPart(
            ref="C1",
            symbol="Test:Capacitor",
            value="100n",
            footprint="Package:C1",
            role="decoupling",
        ),
    ])
    pin_map = {
        "Test:Controller": [
            {"number": "1", "name": "CORE_SUPPLY", "type": "power_in"},
        ],
        "Test:Capacitor": [
            {"number": "1", "name": "1", "type": "passive"},
            {"number": "2", "name": "2", "type": "passive"},
        ],
    }
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.config.symbol_dir",
        lambda: "/symbols",
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda symbol: pin_map[symbol],
    )
    intent = NetlistIntent(
        nets=[
            NetIntent(
                name="CORE_ISLAND",
                kind="signal",
                pins=[
                    LogicalPin(ref="U1", pin="CORE_SUPPLY"),
                    LogicalPin(ref="C1", pin="1"),
                ],
            ),
            NetIntent(
                name="GND",
                kind="ground",
                pins=[LogicalPin(ref="C1", pin="2")],
            ),
        ],
        supply_nets=["3V3"],
        ground_net="GND",
    )

    checks = SchConnectionsStep().check(state, intent)
    source = next(
        check for check in checks if check.name == "power_input_net_has_source"
    )

    assert not source.ok
    assert "CORE_ISLAND" in source.message
    assert "U1:1(CORE_SUPPLY)" in source.message


def test_power_input_net_accepts_real_power_output(monkeypatch) -> None:
    state = PipelineState(requirement_text="generic controller")
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="Test:Controller",
            value="Controller",
            footprint="Package:U1",
            role="mcu",
        ),
        SelectedPart(
            ref="C1",
            symbol="Test:Capacitor",
            value="100n",
            footprint="Package:C1",
            role="decoupling",
        ),
    ])
    pin_map = {
        "Test:Controller": [
            {"number": "1", "name": "VREG_OUT", "type": "power_out"},
            {"number": "2", "name": "CORE_SUPPLY", "type": "power_in"},
        ],
        "Test:Capacitor": [
            {"number": "1", "name": "1", "type": "passive"},
            {"number": "2", "name": "2", "type": "passive"},
        ],
    }
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.config.symbol_dir",
        lambda: "/symbols",
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda symbol: pin_map[symbol],
    )
    intent = NetlistIntent(
        nets=[
            NetIntent(
                name="CORE",
                kind="signal",
                pins=[
                    LogicalPin(ref="U1", pin="VREG_OUT"),
                    LogicalPin(ref="U1", pin="CORE_SUPPLY"),
                    LogicalPin(ref="C1", pin="1"),
                ],
            ),
            NetIntent(
                name="GND",
                kind="ground",
                pins=[LogicalPin(ref="C1", pin="2")],
            ),
        ],
        ground_net="GND",
    )

    checks = SchConnectionsStep().check(state, intent)

    assert next(
        check for check in checks if check.name == "power_input_net_has_source"
    ).ok


def test_pin_name_and_number_aliases_collapse_to_one_physical_endpoint(
    monkeypatch,
) -> None:
    state = PipelineState(requirement_text="x")
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="Test:Regulator",
            value="REG",
            footprint="Package:U1",
            role="regulator",
        ),
    ])
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.config.symbol_dir",
        lambda: "/symbols",
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda _symbol: [
            {"number": "5", "name": "VOUT", "type": "power_out"},
        ],
    )
    intent = NetlistIntent(
        nets=[
            NetIntent(
                name="REG_OUT",
                kind="power",
                pins=[
                    LogicalPin(ref="U1", pin="VOUT"),
                    LogicalPin(ref="U1", pin="5"),
                ],
            ),
        ],
        supply_nets=["REG_OUT"],
        ground_net="GND",
    )

    checks = SchConnectionsStep().check(state, intent)

    assert not next(
        check
        for check in checks
        if check.name == "no_single_physical_pin_nets"
    ).ok
    assert next(
        check
        for check in checks
        if check.name == "single_power_output_per_net"
    ).ok


def test_positive_power_input_on_ground_is_rejected(monkeypatch) -> None:
    state = PipelineState(requirement_text="x")
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="Test:MCU",
            value="MCU",
            footprint="Package:U1",
            role="mcu",
        ),
        SelectedPart(
            ref="C1",
            symbol="Test:C",
            value="100n",
            footprint="Package:C1",
            role="decoupling",
        ),
    ])
    pin_map = {
        "Test:MCU": [
            {"number": "1", "name": "ADC_AVDD", "type": "power_in"},
            {"number": "2", "name": "GND", "type": "power_in"},
        ],
        "Test:C": [
            {"number": "1", "name": "1", "type": "passive"},
            {"number": "2", "name": "2", "type": "passive"},
        ],
    }
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.config.symbol_dir",
        lambda: "/symbols",
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda symbol: pin_map[symbol],
    )
    intent = NetlistIntent(
        nets=[
            NetIntent(
                name="3V3",
                kind="power",
                pins=[
                    LogicalPin(ref="U1", pin="GND"),
                    LogicalPin(ref="C1", pin="1"),
                ],
            ),
            NetIntent(
                name="GND",
                kind="ground",
                pins=[
                    LogicalPin(ref="U1", pin="ADC_AVDD"),
                    LogicalPin(ref="C1", pin="2"),
                ],
            ),
        ],
        supply_nets=["3V3"],
        ground_net="GND",
    )

    checks = SchConnectionsStep().check(state, intent)
    polarity = next(
        check for check in checks if check.name == "power_pin_rail_polarity"
    )

    assert not polarity.ok
    assert "ADC_AVDD" in polarity.message
    assert "GND" in polarity.message


def _selected_state() -> PipelineState:
    state = PipelineState(requirement_text="tiny")
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="Device:R",
            value="controller",
            footprint="Resistor_SMD:R_0603_1608Metric",
            role="controller",
        ),
        SelectedPart(
            ref="C1",
            symbol="Device:R",
            value="decoupling",
            footprint="Resistor_SMD:R_0603_1608Metric",
            role="decoupling",
        ),
    ])
    state.results.append(
        StepResult(
            step=PipelineStep.SELECTION,
            summary="2 parts",
        )
    )
    return state


def _mock_grounded_libraries(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.config.symbol_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.config.footprint_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.grounding.ground_symbol",
        lambda lib_id: lib_id,
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.grounding.ground_footprint",
        lambda lib_id: lib_id,
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.resolve_symbol",
        lambda lib_id: object() if lib_id == "Device:R" else None,
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: (
            [
                {"number": "1", "name": "~"},
                {"number": "2", "name": "~"},
            ]
            if lib_id == "Device:R"
            else None
        ),
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_properties",
        lambda lib_id: (
            {
                "Value": "R",
                "Description": "Resistor",
                "Footprint": "",
            }
            if lib_id == "Device:R"
            else {}
        ),
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.footprints.footprint_pads",
        lambda lib_id: (
            [{"number": "1"}, {"number": "2"}]
            if lib_id == "Resistor_SMD:R_0603_1608Metric"
            else None
        ),
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline._ground_mpns",
        lambda _parts: None,
    )


def test_offline_connectivity_yields_no_nets_instead_of_another_board() -> None:
    state = PipelineState(requirement_text="ATmega328 USB-C 3.3V 8MHz dev board")
    SchConnectionsStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    intent = state.artifact(PipelineStep.SCH_CONNECTIONS)
    assert isinstance(intent, NetlistIntent)
    # No proposal means no connectivity — not the ATmega reference netlist,
    # which used to stand in here and satisfy every downstream check.
    assert intent.nets == []
    assert intent.supply_nets == []
    result = state.results[-1]
    assert result.blocked
    assert [c.name for c in result.checks if not c.ok]


def test_connections_block_selected_component_omitted_from_all_nets(
    monkeypatch,
    tmp_path,
) -> None:
    _mock_grounded_libraries(monkeypatch, tmp_path)
    state = PipelineState(requirement_text="three resistors")
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(
            ref=ref,
            symbol="Device:R",
            value="10k",
            footprint="Resistor_SMD:R_0603_1608Metric",
            role="resistor",
        )
        for ref in ("R1", "R2", "R3")
    ])
    intent = NetlistIntent(
        nets=[
            NetIntent(
                name="3V3",
                kind="power",
                pins=[
                    LogicalPin(ref="R1", pin="1"),
                    LogicalPin(ref="R2", pin="1"),
                ],
            ),
            NetIntent(
                name="GND",
                kind="ground",
                pins=[
                    LogicalPin(ref="R1", pin="2"),
                    LogicalPin(ref="R2", pin="2"),
                ],
            ),
        ],
        supply_nets=["3V3"],
        ground_net="GND",
    )

    checks = SchConnectionsStep().check(state, intent)

    used = next(c for c in checks if c.name == "selected_components_used")
    accounted = next(c for c in checks if c.name == "component_pins_accounted")
    assert not used.ok
    assert "R3" in used.message
    assert not accounted.ok
    assert "R3:1" in accounted.message


def test_fake_llm_connectivity_parsed() -> None:
    net = json.dumps(
        {
            "nets": [
                {"name": "3V3", "kind": "power",
                 "pins": [{"ref": "U1", "pin": "OUT"}, {"ref": "C1", "pin": "1"}]},
                {"name": "GND", "kind": "ground",
                 "pins": [{"ref": "U1", "pin": "GND"}, {"ref": "C1", "pin": "2"}]},
            ],
            "supply_nets": ["3V3"],
            "ground_net": "GND",
            "rationale": "llm",
        }
    )
    state = PipelineState(requirement_text="tiny")
    SchConnectionsStep().run(state, PipelineContext(mode=LlmMode.AUTO, client=FakeLLM([net])))
    result = state.results[-1]
    assert not result.blocked
    assert result.used_llm
    intent = state.artifact(PipelineStep.SCH_CONNECTIONS)
    assert isinstance(intent, NetlistIntent) and intent.rationale == "llm"


def test_single_pin_net_blocks() -> None:
    net = json.dumps(
        {
            "nets": [
                {"name": "3V3", "kind": "power", "pins": [{"ref": "U1", "pin": "OUT"}]},
                {"name": "GND", "kind": "ground",
                 "pins": [{"ref": "U1", "pin": "GND"}, {"ref": "C1", "pin": "2"}]},
            ],
            "supply_nets": ["3V3"],
            "ground_net": "GND",
        }
    )
    state = PipelineState(requirement_text="bad")
    SchConnectionsStep().run(state, PipelineContext(mode=LlmMode.AUTO, client=FakeLLM([net])))
    result = state.results[-1]
    assert result.blocked
    assert any(c.name == "no_single_pin_nets" and not c.ok for c in result.checks)


def test_missing_ground_blocks() -> None:
    net = json.dumps(
        {
            "nets": [
                {"name": "3V3", "kind": "power",
                 "pins": [{"ref": "U1", "pin": "OUT"}, {"ref": "C1", "pin": "1"}]},
            ],
            "supply_nets": ["3V3"],
            "ground_net": "GND",
        }
    )
    state = PipelineState(requirement_text="no gnd")
    SchConnectionsStep().run(state, PipelineContext(mode=LlmMode.AUTO, client=FakeLLM([net])))
    result = state.results[-1]
    assert result.blocked
    assert any(c.name == "has_ground_net" and not c.ok for c in result.checks)


def test_shared_pin_across_distinct_nets_is_reported_as_a_short() -> None:
    intent = NetlistIntent(
        nets=[
            NetIntent(
                name="3V3",
                kind="power",
                pins=[
                    LogicalPin(ref="U1", pin="VOUT"),
                    LogicalPin(ref="C1", pin="1"),
                ],
            ),
            NetIntent(
                name="RUN_PU",
                pins=[
                    LogicalPin(ref="U1", pin="VOUT"),
                    LogicalPin(ref="R1", pin="1"),
                ],
            ),
            NetIntent(
                name="GND",
                kind="ground",
                pins=[
                    LogicalPin(ref="U1", pin="GND"),
                    LogicalPin(ref="C1", pin="2"),
                ],
            ),
        ],
        supply_nets=["3V3"],
        ground_net="GND",
    )

    checks = SchConnectionsStep().check(
        PipelineState(requirement_text="short check"),
        intent,
    )

    assert any(
        check.name == "no_pin_on_multiple_nets" and not check.ok
        for check in checks
    )


def test_different_logical_aliases_of_one_physical_pin_are_a_short(
    monkeypatch,
    tmp_path,
) -> None:
    _mock_grounded_libraries(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda _lib_id: [
            {"number": "1", "name": "OUT"},
            {"number": "2", "name": "GND"},
        ],
    )
    state = PipelineState(requirement_text="alias short")
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="Device:R",
            value="controller",
            footprint="Resistor_SMD:R_0603_1608Metric",
            role="controller",
        ),
        SelectedPart(
            ref="R1",
            symbol="Device:R",
            value="10k",
            footprint="Resistor_SMD:R_0603_1608Metric",
            role="load",
        ),
    ])
    intent = NetlistIntent(
        nets=[
            NetIntent(
                name="RAIL",
                kind="power",
                pins=[
                    LogicalPin(ref="U1", pin="1"),
                    LogicalPin(ref="R1", pin="1"),
                ],
            ),
            NetIntent(
                name="SIGNAL",
                pins=[
                    LogicalPin(ref="U1", pin="OUT"),
                    LogicalPin(ref="R1", pin="2"),
                ],
            ),
            NetIntent(
                name="GND",
                kind="ground",
                pins=[
                    LogicalPin(ref="U1", pin="2"),
                    LogicalPin(ref="R1", pin="2"),
                ],
            ),
        ],
        supply_nets=["RAIL"],
        ground_net="GND",
    )

    checks = SchConnectionsStep().check(state, intent)

    assert any(
        check.name == "no_physical_pin_on_multiple_nets" and not check.ok
        for check in checks
    )


def test_netlist_patch_moves_only_targeted_pins_and_preserves_other_nets() -> None:
    plan = NetlistIntent(
        nets=[
            NetIntent(
                name="GND",
                kind="ground",
                pins=[
                    LogicalPin(ref="U1", pin="1"),
                    LogicalPin(ref="C1", pin="2"),
                ],
            ),
            NetIntent(
                name="SIGNAL",
                pins=[
                    LogicalPin(ref="U1", pin="2"),
                    LogicalPin(ref="R1", pin="1"),
                ],
            ),
        ],
        supply_nets=[],
        ground_net="GND",
    )
    patch = NetlistPatch(
        upsert_nets=[
            NetIntent(
                name="SIGNAL",
                pins=[LogicalPin(ref="U1", pin="1")],
            )
        ],
        add_no_connect_pins=[LogicalPin(ref="U1", pin="3")],
    )

    repaired = _apply_netlist_patch(plan, patch)

    assert {pin.key() for pin in repaired.net("GND").pins} == {"C1:2"}
    assert {pin.key() for pin in repaired.net("SIGNAL").pins} == {
        "U1:1",
        "U1:2",
        "R1:1",
    }
    assert [pin.key() for pin in repaired.no_connect_pins] == ["U1:3"]


def test_connection_repair_can_declare_and_merge_required_support_part(
    monkeypatch,
    tmp_path,
) -> None:
    _mock_grounded_libraries(monkeypatch, tmp_path)
    first = json.dumps({
        "nets": [
            {
                "name": "3V3",
                "kind": "power",
                "pins": [
                    {"ref": "U1", "pin": "1"},
                    {"ref": "C1", "pin": "1"},
                    {"ref": "R28", "pin": "1"},
                ],
            },
            {
                "name": "GND",
                "kind": "ground",
                "pins": [
                    {"ref": "U1", "pin": "2"},
                    {"ref": "C1", "pin": "2"},
                ],
            },
        ],
        "supply_nets": ["3V3"],
        "ground_net": "GND",
    })
    repaired = json.dumps({
        "additional_parts": [
            {
                "ref": "R28",
                "symbol": "Device:R",
                "value": "100k",
                "footprint": "Resistor_SMD:R_0603_1608Metric",
                "role": "frequency_set_resistor",
            }
        ],
        "upsert_nets": [
            {
                "name": "GND",
                "kind": "ground",
                "pins": [
                    {"ref": "R28", "pin": "2"},
                ],
            },
        ],
    })
    client = FakeLLM([first, repaired])
    state = _selected_state()

    result = SchConnectionsStep().run(
        state,
        PipelineContext(
            mode=LlmMode.AUTO,
            client=client,
            repair_attempts=1,
        ),
    )

    assert not result.blocked
    assert len(client.prompts) == 2
    assert "U1 role='controller' value='controller'" in client.prompts[0]
    assert "pins_reference_selected_parts" in client.prompts[1]
    assert "compact failure scope" in client.prompts[1]
    assert '"related_refs": ["R28"]' in client.prompts[1]
    selection = state.artifact(PipelineStep.SELECTION)
    assert isinstance(selection, SelectionPlan)
    assert {part.ref for part in selection.parts} == {"U1", "C1", "R28"}
    assert state.results[0].summary == "3 parts (0 grounded to a catalog MPN)"


def test_connection_stage_part_growth_has_a_small_bounded_budget() -> None:
    state = PipelineState(requirement_text="complex board")
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(
            ref=f"R{index}",
            symbol="Device:R",
            value="10k",
            footprint="Resistor_SMD:R_0603_1608Metric",
            role=f"selected_resistor_{index}",
        )
        for index in range(1, 33)
    ])
    intent = NetlistIntent(
        additional_parts=[
            SelectedPart(
                ref=f"R{index}",
                symbol="Device:R",
                value="10k",
                footprint="Resistor_SMD:R_0603_1608Metric",
                role=f"support_resistor_{index}",
            )
            for index in range(99, 108)
        ],
        nets=[
            NetIntent(
                name="3V3",
                kind="power",
                pins=[
                    LogicalPin(ref="R1", pin="1"),
                    LogicalPin(ref="R2", pin="1"),
                ],
            ),
            NetIntent(
                name="GND",
                kind="ground",
                pins=[
                    LogicalPin(ref="R1", pin="2"),
                    LogicalPin(ref="R2", pin="2"),
                ],
            ),
        ],
        supply_nets=["3V3"],
        ground_net="GND",
    )

    checks = SchConnectionsStep().check(state, intent)

    assert any(
        check.name == "additional_part_budget" and not check.ok
        for check in checks
    )


def test_additional_two_pin_part_must_connect_both_terminals(
    monkeypatch,
    tmp_path,
) -> None:
    _mock_grounded_libraries(monkeypatch, tmp_path)
    proposal = json.dumps({
        "additional_parts": [
            {
                "ref": "R28",
                "symbol": "Device:R",
                "value": "100k",
                "footprint": "Resistor_SMD:R_0603_1608Metric",
                "role": "frequency_set_resistor",
            }
        ],
        "nets": [
            {
                "name": "3V3",
                "kind": "power",
                "pins": [
                    {"ref": "U1", "pin": "1"},
                    {"ref": "C1", "pin": "1"},
                    {"ref": "R28", "pin": "1"},
                ],
            },
            {
                "name": "GND",
                "kind": "ground",
                "pins": [
                    {"ref": "U1", "pin": "2"},
                    {"ref": "C1", "pin": "2"},
                ],
            },
        ],
        "supply_nets": ["3V3"],
        "ground_net": "GND",
    })
    state = _selected_state()

    result = SchConnectionsStep().run(
        state,
        PipelineContext(mode=LlmMode.AUTO, client=FakeLLM([proposal])),
    )

    assert result.blocked
    terminal_check = next(
        check
        for check in result.checks
        if check.name == "additional_two_pin_parts_fully_connected"
    )
    assert not terminal_check.ok
    selection = state.artifact(PipelineStep.SELECTION)
    assert isinstance(selection, SelectionPlan)
    assert {part.ref for part in selection.parts} == {"U1", "C1"}


def test_additional_part_must_pass_selection_grounding(
    monkeypatch,
    tmp_path,
) -> None:
    _mock_grounded_libraries(monkeypatch, tmp_path)
    proposal = json.dumps({
        "additional_parts": [
            {
                "ref": "R28",
                "symbol": "Device:Invented",
                "value": "100k",
                "footprint": "Resistor_SMD:R_0603_1608Metric",
                "role": "frequency_set_resistor",
            }
        ],
        "nets": [
            {
                "name": "3V3",
                "kind": "power",
                "pins": [
                    {"ref": "U1", "pin": "1"},
                    {"ref": "C1", "pin": "1"},
                    {"ref": "R28", "pin": "1"},
                ],
            },
            {
                "name": "GND",
                "kind": "ground",
                "pins": [
                    {"ref": "U1", "pin": "2"},
                    {"ref": "C1", "pin": "2"},
                    {"ref": "R28", "pin": "2"},
                ],
            },
        ],
        "supply_nets": ["3V3"],
        "ground_net": "GND",
    })
    state = _selected_state()

    result = SchConnectionsStep().run(
        state,
        PipelineContext(mode=LlmMode.AUTO, client=FakeLLM([proposal])),
    )

    assert result.blocked
    assert any(
        check.name == "additional_parts:symbol:R28" and not check.ok
        for check in result.checks
    )


def test_functional_checks_reject_abandoned_ic_pins_and_grounded_crystal(
    monkeypatch,
) -> None:
    pin_map = {
        "Test:Crystal": [
            {"number": "1", "name": "1", "type": "passive"},
            {"number": "2", "name": "2", "type": "passive"},
        ],
        "Test:Buck": [
            {"number": "1", "name": "BOOT", "type": "input"},
            {"number": "2", "name": "SW", "type": "output"},
            {"number": "3", "name": "GND", "type": "power_in"},
        ],
    }
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="Y1",
            symbol="Test:Crystal",
            value="8MHz",
            role="hse_crystal_8mhz",
        ),
        SelectedPart(
            ref="U2",
            symbol="Test:Buck",
            value="buck",
            role="buck_converter_7_24v_to_5v",
        ),
    ])
    intent = NetlistIntent(
        nets=[
            NetIntent(
                name="HSE_IN",
                pins=[
                    LogicalPin(ref="Y1", pin="1"),
                    LogicalPin(ref="U2", pin="2"),
                ],
            ),
            NetIntent(
                name="GND",
                kind="ground",
                pins=[
                    LogicalPin(ref="Y1", pin="2"),
                    LogicalPin(ref="U2", pin="3"),
                ],
            ),
        ],
        no_connect_pins=[LogicalPin(ref="U2", pin="1")],
        ground_net="GND",
    )

    checks = _functional_connection_checks(selection, intent)

    critical = next(
        check for check in checks
        if check.name == "critical_power_reset_pins_connected"
    )
    crystal = next(
        check for check in checks
        if check.name == "crystal_two_distinct_signal_nets:Y1"
    )
    assert not critical.ok
    assert "U2:1(BOOT)" in critical.message
    assert not crystal.ok


def test_functional_checks_reject_parallel_led_and_wrong_swd_mapping(
    monkeypatch,
) -> None:
    pin_map = {
        "Test:TwoPin": [
            {"number": "1", "name": "1"},
            {"number": "2", "name": "2"},
        ],
        "Test:MCU": [
            {"number": "1", "name": "PA13/JTMS-SWDIO"},
            {"number": "2", "name": "PA14/JTCK-SWCLK"},
            {"number": "3", "name": "NRST"},
        ],
        "Test:SWD": [
            {"number": str(number), "name": str(number)}
            for number in range(1, 11)
        ],
    }
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="U1", symbol="Test:MCU", value="MCU", role="mcu",
        ),
        SelectedPart(
            ref="J4", symbol="Test:SWD", value="SWD", role="cortex_swd_10_pin",
        ),
        SelectedPart(
            ref="D3", symbol="Test:TwoPin", value="LED", role="power_status_led",
        ),
        SelectedPart(
            ref="R5",
            symbol="Test:TwoPin",
            value="1k",
            role="led_power_current_limit",
        ),
    ])
    intent = NetlistIntent(
        nets=[
            NetIntent(
                name="3V3",
                kind="power",
                pins=[
                    LogicalPin(ref="D3", pin="1"),
                    LogicalPin(ref="R5", pin="1"),
                    LogicalPin(ref="J4", pin="1"),
                ],
            ),
            NetIntent(
                name="GND",
                kind="ground",
                pins=[
                    LogicalPin(ref="D3", pin="2"),
                    LogicalPin(ref="R5", pin="2"),
                    LogicalPin(ref="J4", pin="3"),
                    LogicalPin(ref="J4", pin="5"),
                    LogicalPin(ref="J4", pin="9"),
                ],
            ),
            NetIntent(
                name="SWDIO",
                pins=[
                    LogicalPin(ref="U1", pin="1"),
                    LogicalPin(ref="J4", pin="7"),
                ],
            ),
            NetIntent(
                name="SWCLK",
                pins=[
                    LogicalPin(ref="U1", pin="2"),
                    LogicalPin(ref="J4", pin="9"),
                ],
            ),
            NetIntent(
                name="NRST",
                pins=[
                    LogicalPin(ref="U1", pin="3"),
                    LogicalPin(ref="J4", pin="5"),
                ],
            ),
        ],
        supply_nets=["3V3"],
        ground_net="GND",
    )

    checks = _functional_connection_checks(selection, intent)

    led = next(
        check for check in checks
        if check.name == "led_current_limit_in_series:D3"
    )
    swd = next(
        check for check in checks
        if check.name == "cortex_swd_10pin_mapping:J4"
    )
    assert not led.ok
    assert not swd.ok
    assert "pin2" in swd.message


def test_functional_checks_accept_color_named_led_resistor_and_semantic_swd_nets(
    monkeypatch,
) -> None:
    pin_map = {
        "Test:TwoPin": [
            {"number": "1", "name": "1"},
            {"number": "2", "name": "2"},
        ],
        # Real STM32 KiCad symbols commonly expose only GPIO names here, while
        # SWD is an alternate function documented outside the symbol.
        "Test:MCU": [
            {"number": "1", "name": "PA13"},
            {"number": "2", "name": "PA14"},
            {"number": "3", "name": "NRST"},
            {"number": "4", "name": "GPIO_LED"},
        ],
        "Test:SWD": [
            {"number": str(number), "name": str(number)}
            for number in range(1, 11)
        ],
    }
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )
    selection = SelectionPlan(parts=[
        SelectedPart(ref="U1", symbol="Test:MCU", value="MCU", role="mcu"),
        SelectedPart(
            ref="J4", symbol="Test:SWD", value="SWD", role="cortex_swd_10_pin",
        ),
        SelectedPart(
            ref="LED1",
            symbol="Test:TwoPin",
            value="LED",
            role="power_status_led",
        ),
        SelectedPart(
            ref="R13",
            symbol="Test:TwoPin",
            value="1k",
            role="led_current_limit_red",
        ),
    ])

    def net(name: str, *pins: tuple[str, str], kind: str = "signal") -> NetIntent:
        return NetIntent(
            name=name,
            kind=kind,
            pins=[LogicalPin(ref=ref, pin=pin) for ref, pin in pins],
        )

    intent = NetlistIntent(
        nets=[
            net("3V3", ("J4", "1"), ("R13", "1"), kind="power"),
            net("LED_POWER_MID", ("R13", "2"), ("LED1", "1")),
            net("GND", ("LED1", "2"), ("J4", "3"), ("J4", "5"), ("J4", "9"), kind="ground"),
            net("SWD_SWDIO", ("U1", "1"), ("J4", "2")),
            net("SWD_SWCLK", ("U1", "2"), ("J4", "4")),
            net("NRST", ("U1", "3"), ("J4", "10")),
        ],
        no_connect_pins=[
            LogicalPin(ref="J4", pin=number)
            for number in ("6", "7", "8")
        ],
        supply_nets=["3V3"],
        ground_net="GND",
    )

    checks = _functional_connection_checks(selection, intent)
    led = next(
        check for check in checks
        if check.name == "led_current_limit_in_series:LED1"
    )
    swd = next(
        check for check in checks
        if check.name == "cortex_swd_10pin_mapping:J4"
    )

    assert led.ok
    assert swd.ok


def test_functional_checks_accept_led_resistor_named_series(monkeypatch) -> None:
    """A correctly wired series resistor must pass whatever the role is called.

    The candidate filter used to require the words "current"/"limit" in the
    role. A real run named it ``led_series_resistor``, which emptied the
    candidate list and failed the check on this exact, correct topology:
    VDD33 -> R4 -> LED_SERIES -> D1:A, D1:K -> GND.
    """
    pin_map = {
        "Device:LED": [
            {"number": "1", "name": "A"},
            {"number": "2", "name": "K"},
        ],
        "Device:R": [
            {"number": "1", "name": "1"},
            {"number": "2", "name": "2"},
        ],
    }
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )
    selection = SelectionPlan(parts=[
        SelectedPart(ref="D1", symbol="Device:LED", value="LED", role="status_led"),
        SelectedPart(
            ref="R4", symbol="Device:R", value="1k", role="led_series_resistor",
        ),
    ])
    intent = NetlistIntent(
        nets=[
            NetIntent(name="VDD33", kind="power", pins=[LogicalPin(ref="R4", pin="1")]),
            NetIntent(name="LED_SERIES", pins=[
                LogicalPin(ref="R4", pin="2"), LogicalPin(ref="D1", pin="A")]),
            NetIntent(name="GND", kind="ground", pins=[LogicalPin(ref="D1", pin="K")]),
        ],
        supply_nets=["VDD33"], ground_net="GND",
    )

    checks = _functional_connection_checks(selection, intent)
    led = next(
        check for check in checks
        if check.name == "led_current_limit_in_series:D1"
    )
    assert led.ok, led.message
    assert "R4" in led.message


def test_functional_checks_still_reject_parallel_led_named_series(monkeypatch) -> None:
    """Dropping the role keywords must not weaken the parallel-wiring check."""
    pin_map = {
        "Device:LED": [
            {"number": "1", "name": "A"},
            {"number": "2", "name": "K"},
        ],
        "Device:R": [
            {"number": "1", "name": "1"},
            {"number": "2", "name": "2"},
        ],
    }
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )
    selection = SelectionPlan(parts=[
        SelectedPart(ref="D1", symbol="Device:LED", value="LED", role="status_led"),
        SelectedPart(
            ref="R4", symbol="Device:R", value="1k", role="led_series_resistor",
        ),
    ])
    intent = NetlistIntent(
        nets=[
            NetIntent(name="VDD33", kind="power", pins=[
                LogicalPin(ref="R4", pin="1"), LogicalPin(ref="D1", pin="A")]),
            NetIntent(name="GND", kind="ground", pins=[
                LogicalPin(ref="R4", pin="2"), LogicalPin(ref="D1", pin="K")]),
        ],
        supply_nets=["VDD33"], ground_net="GND",
    )

    checks = _functional_connection_checks(selection, intent)
    led = next(
        check for check in checks
        if check.name == "led_current_limit_in_series:D1"
    )
    assert not led.ok


def _two_pin_map() -> dict[str, list[dict[str, str]]]:
    return {
        "Test:TwoPin": [
            {"number": "1", "name": "1"},
            {"number": "2", "name": "2"},
        ],
        "Test:ThreePin": [
            {"number": "1", "name": "GND"},
            {"number": "2", "name": "VO"},
            {"number": "3", "name": "VI"},
        ],
        "Test:SixPin": [
            {"number": str(n), "name": str(n)} for n in range(1, 7)
        ],
        "Test:NoPin": [],
        "Test:OnePin": [{"number": "1", "name": "1"}],
    }


def _patch_pins(monkeypatch) -> None:
    pin_map = _two_pin_map()
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )


def _net(name: str, *pins: tuple[str, str], kind: str = "signal") -> NetIntent:
    return NetIntent(
        name=name,
        kind=kind,
        pins=[LogicalPin(ref=ref, pin=pin) for ref, pin in pins],
    )


def test_two_terminal_short_blocks_bridged_fuse(monkeypatch) -> None:
    """A polyfuse with both terminals on the 5 V input is a wire, not a fuse.

    KiCad ERC stays silent: no net is shorted to another net, only the
    overcurrent protection is silently defeated.
    """
    _patch_pins(monkeypatch)
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="F1", symbol="Test:TwoPin", value="500mA",
            role="input_protection_fuse",
        ),
    ])
    intent = NetlistIntent(
        nets=[_net("net_5V", ("F1", "1"), ("F1", "2"), kind="power")],
        supply_nets=["net_5V"], ground_net="GND",
    )

    checks = _functional_connection_checks(selection, intent)
    shorted = next(
        c for c in checks if c.name == "two_terminal_not_shorted:F1"
    )
    assert not shorted.ok
    assert "net_5V" in shorted.message


def test_two_terminal_short_accepts_distinct_nets(monkeypatch) -> None:
    _patch_pins(monkeypatch)
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="F1", symbol="Test:TwoPin", value="500mA",
            role="input_protection_fuse",
        ),
    ])
    intent = NetlistIntent(
        nets=[
            _net("net_5V", ("F1", "1"), kind="power"),
            _net("net_FUSED", ("F1", "2"), kind="power"),
        ],
        supply_nets=["net_5V", "net_FUSED"], ground_net="GND",
    )

    checks = _functional_connection_checks(selection, intent)
    assert next(c for c in checks if c.name == "two_terminal_not_shorted:F1").ok


def test_two_terminal_short_skips_half_connected_part(monkeypatch) -> None:
    """A single wired terminal belongs to the dangling-pin checks, not here."""
    _patch_pins(monkeypatch)
    selection = SelectionPlan(parts=[
        SelectedPart(ref="C1", symbol="Test:TwoPin", value="100nF", role="decoupling"),
    ])
    intent = NetlistIntent(
        nets=[_net("net_3V3", ("C1", "1"), kind="power")],
        supply_nets=["net_3V3"], ground_net="GND",
    )

    checks = _functional_connection_checks(selection, intent)
    assert not [c for c in checks if c.name.startswith("two_terminal_not_shorted")]


def test_two_terminal_short_ignores_non_two_pin_parts(monkeypatch) -> None:
    _patch_pins(monkeypatch)
    selection = SelectionPlan(parts=[
        SelectedPart(ref="U2", symbol="Test:ThreePin", value="AMS1117", role="regulator"),
    ])
    intent = NetlistIntent(
        nets=[_net("net_5V", ("U2", "VI"), ("U2", "VO"), kind="power")],
        supply_nets=["net_5V"], ground_net="GND",
    )

    checks = _functional_connection_checks(selection, intent)
    assert not [c for c in checks if c.name.startswith("two_terminal_not_shorted")]


def test_mechanical_part_blocks_relabelled_oscillator(monkeypatch) -> None:
    """Guards the four oscillators that shipped as M2 mounting holes.

    Role and value both read "mounting hole"; only the grounded symbol showed a
    6-pin electrical device, wired into the 3.3 V rail.
    """
    _patch_pins(monkeypatch)
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="M1", symbol="Test:SixPin", value="MountingHole_M2",
            role="mounting_hole",
        ),
    ])
    intent = NetlistIntent(
        nets=[
            _net("net_3V3", ("M1", "6"), kind="power"),
            _net("GND", ("M1", "3"), kind="ground"),
        ],
        supply_nets=["net_3V3"], ground_net="GND",
    )

    checks = _functional_connection_checks(selection, intent)
    fake = next(
        c for c in checks if c.name == "mechanical_part_not_electrical:M1"
    )
    assert not fake.ok
    assert "net_3V3" in fake.message
    assert "6 pins" in fake.message


def test_mechanical_part_accepts_pinless_hole(monkeypatch) -> None:
    _patch_pins(monkeypatch)
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="M1", symbol="Test:NoPin", value="MountingHole_M2",
            role="mounting_hole",
        ),
    ])
    intent = NetlistIntent(
        nets=[_net("GND", ("M1", "1"), kind="ground")],
        supply_nets=[], ground_net="GND",
    )

    checks = _functional_connection_checks(selection, intent)
    assert next(
        c for c in checks if c.name == "mechanical_part_not_electrical:M1"
    ).ok


def test_mechanical_part_accepts_single_pad_stitched_to_ground(monkeypatch) -> None:
    """A grounded mounting pad is standard practice and must not be flagged."""
    _patch_pins(monkeypatch)
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="M1", symbol="Test:OnePin", value="MountingHole_Pad_M2",
            role="mounting_hole",
        ),
    ])
    intent = NetlistIntent(
        nets=[_net("GND", ("M1", "1"), kind="ground")],
        supply_nets=[], ground_net="GND",
    )

    checks = _functional_connection_checks(selection, intent)
    assert next(
        c for c in checks if c.name == "mechanical_part_not_electrical:M1"
    ).ok


def test_mechanical_part_ignores_electrical_roles(monkeypatch) -> None:
    _patch_pins(monkeypatch)
    selection = SelectionPlan(parts=[
        SelectedPart(ref="U3", symbol="Test:SixPin", value="Si512A", role="oscillator"),
    ])
    intent = NetlistIntent(
        nets=[_net("net_3V3", ("U3", "6"), kind="power")],
        supply_nets=["net_3V3"], ground_net="GND",
    )

    checks = _functional_connection_checks(selection, intent)
    assert not [
        c for c in checks if c.name.startswith("mechanical_part_not_electrical")
    ]


@pytest.mark.parametrize("protection_node", ["sense", "divider"])
def test_analog_chain_accepts_upper_lower_roles_and_adc_after_current_limit(
    monkeypatch,
    protection_node: str,
) -> None:
    pin_map = {
        "Test:MCU": [{"number": "1", "name": "PA0"}],
        "Test:TwoPin": [
            {"number": "1", "name": "1"},
            {"number": "2", "name": "2"},
        ],
    }
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )

    def part(ref: str, role: str, *, mcu: bool = False) -> SelectedPart:
        return SelectedPart(
            ref=ref,
            symbol="Test:MCU" if mcu else "Test:TwoPin",
            value=role,
            role=role,
        )

    selection = SelectionPlan(parts=[
        part("U1", "mcu", mcu=True),
        part("R1", "analog_input_1_divider_upper"),
        part("R2", "analog_input_1_divider_lower"),
        part("R3", "analog_input_1_current_limit"),
        part("C1", "analog_input_1_filter_capacitor"),
        part("D1", "analog_input_overvoltage_protection_1"),
    ])

    def net(name: str, *pins: tuple[str, str], kind: str = "signal") -> NetIntent:
        return NetIntent(
            name=name,
            kind=kind,
            pins=[LogicalPin(ref=ref, pin=pin) for ref, pin in pins],
        )

    intent = NetlistIntent(
        nets=[
            net("ANALOG_RAW", ("R1", "1"), ("J1", "1")),
            net(
                "ANALOG_DIV",
                ("R1", "2"),
                ("R2", "1"),
                ("R3", "1"),
                *((("D1", "1"),) if protection_node == "divider" else ()),
            ),
            net(
                "ANALOG_SENSE",
                ("R3", "2"),
                ("C1", "1"),
                ("U1", "1"),
                *((("D1", "1"),) if protection_node == "sense" else ()),
            ),
            net("GND", ("R2", "2"), ("C1", "2"), ("D1", "2"), kind="ground"),
        ],
        ground_net="GND",
    )

    check = next(
        check
        for check in _functional_connection_checks(selection, intent)
        if check.name == "analog_input_safe_chain:1"
    )

    assert check.ok


def test_power_pins_must_use_declared_ground_and_supply_rails(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda _lib_id: [
            {"number": "1", "name": "VSS"},
            {"number": "2", "name": "VDD"},
        ],
    )
    selection = SelectionPlan(parts=[
        SelectedPart(ref="U1", symbol="Test:MCU", value="MCU", role="mcu"),
    ])

    def intent(ground_name: str) -> NetlistIntent:
        return NetlistIntent(
            nets=[
                NetIntent(
                    name=ground_name,
                    kind="signal" if ground_name != "GND" else "ground",
                    pins=[LogicalPin(ref="U1", pin="1")],
                ),
                NetIntent(
                    name="3V3",
                    kind="power",
                    pins=[LogicalPin(ref="U1", pin="2")],
                ),
            ],
            supply_nets=["3V3"],
            ground_net="GND",
        )

    misplaced = next(
        check
        for check in _functional_connection_checks(
            selection,
            intent("LED_GND"),
        )
        if check.name == "power_pin_rail_class"
    )
    correct = next(
        check
        for check in _functional_connection_checks(selection, intent("GND"))
        if check.name == "power_pin_rail_class"
    )

    assert not misplaced.ok
    assert "U1:1(VSS)->LED_GND" in misplaced.message
    assert correct.ok


def test_functional_checks_accept_complete_buck_reference_topology(
    monkeypatch,
) -> None:
    pin_map = {
        "Test:Buck": [
            {"number": "1", "name": "BOOT"},
            {"number": "2", "name": "VIN"},
            {"number": "3", "name": "RT/CLK"},
            {"number": "4", "name": "GND"},
            {"number": "5", "name": "SW"},
            {"number": "6", "name": "COMP"},
            {"number": "7", "name": "FB"},
        ],
        "Test:TwoPin": [
            {"number": "1", "name": "1"},
            {"number": "2", "name": "2"},
        ],
    }
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )

    def part(ref: str, role: str) -> SelectedPart:
        return SelectedPart(
            ref=ref,
            symbol="Test:Buck" if ref == "U2" else "Test:TwoPin",
            value=role,
            role=role,
        )

    selection = SelectionPlan(parts=[
        part("U2", "buck_converter_7_24v_to_5v"),
        part("L1", "buck_inductor"),
        part("C1", "buck_input_capacitor"),
        part("C2", "buck_output_capacitor"),
        part("C3", "buck_bootstrap_capacitor"),
        part("R1", "buck_feedback_high"),
        part("R2", "buck_feedback_low"),
        part("R3", "buck_timing_resistor"),
        part("C4", "buck_compensation_capacitor"),
    ])

    def net(name: str, *pins: tuple[str, str], kind: str = "signal") -> NetIntent:
        return NetIntent(
            name=name,
            kind=kind,
            pins=[LogicalPin(ref=ref, pin=pin) for ref, pin in pins],
        )

    intent = NetlistIntent(
        nets=[
            net("VIN", ("U2", "2"), ("C1", "1"), kind="power"),
            net(
                "GND",
                ("U2", "4"),
                ("C1", "2"),
                ("C2", "2"),
                ("R2", "2"),
                ("R3", "2"),
                ("C4", "2"),
                kind="ground",
            ),
            net("SW", ("U2", "5"), ("L1", "1"), ("C3", "2")),
            net("5V", ("L1", "2"), ("C2", "1"), ("R1", "1"), kind="power"),
            net("BOOT", ("U2", "1"), ("C3", "1")),
            net("FB", ("U2", "7"), ("R1", "2"), ("R2", "1")),
            net("RT", ("U2", "3"), ("R3", "1")),
            net("COMP", ("U2", "6"), ("C4", "1")),
        ],
        supply_nets=["VIN", "5V"],
        ground_net="GND",
    )

    checks = _functional_connection_checks(selection, intent)

    buck = next(
        check for check in checks
        if check.name == "buck_reference_topology:U2"
    )
    assert buck.ok, buck.message


def test_datasheet_connection_gate_catches_an_undersized_output_capacitor() -> None:
    """The connections step must judge connected VALUES against the datasheet.

    This is the half of the fact base that the selection step structurally cannot
    reach: before the netlist exists there is no way to tell an input capacitor
    from an output one, so a stability requirement cannot be checked. The AMS1117
    asks for 22 uF of solid tantalum for loop compensation; a 1 uF ceramic - the
    correct choice for an AP2112K - is not a substitute, and only connectivity
    makes that comparison possible.
    """
    selection = SelectionPlan(parts=[
        SelectedPart(ref="U1", symbol="Regulator_Linear:AMS1117-3.3",
                     value="AMS1117-3.3", footprint="Package:SOT-223",
                     role="ldo regulator"),
        SelectedPart(ref="C1", symbol="Device:C", value="1uF",
                     footprint="Package:C", role="output capacitor"),
    ])
    intent = NetlistIntent(
        nets=[
            NetIntent(name="5V", kind="power",
                      pins=[LogicalPin(ref="U1", pin="VI")]),
            NetIntent(name="3V3", kind="power",
                      pins=[LogicalPin(ref="U1", pin="VO"),
                            LogicalPin(ref="C1", pin="1")]),
            NetIntent(name="GND", kind="ground",
                      pins=[LogicalPin(ref="U1", pin="GND"),
                            LogicalPin(ref="C1", pin="2")]),
        ],
        supply_nets=["5V", "3V3"],
        ground_net="GND",
    )

    checks = _functional_connection_checks(selection, intent)
    hits = [c for c in checks if c.name.startswith("datasheet_connection:")]
    assert hits, (
        "the datasheet gate must run in the connections step; got check names "
        f"{[c.name for c in checks]}"
    )
    cout = next(c for c in hits if c.name.endswith("required_cout"))
    assert not cout.ok
    assert "22" in cout.message, "the 22 uF requirement must be quoted"
    assert "AMS1117" in cout.message


def test_datasheet_connection_gate_passes_a_correctly_specified_regulator() -> None:
    """The mirror case: a compliant design must produce a passing check.

    A gate that only ever fires is as useless as one that never does.
    """
    selection = SelectionPlan(parts=[
        SelectedPart(ref="U1", symbol="Regulator_Linear:AMS1117-3.3",
                     value="AMS1117-3.3", footprint="Package:SOT-223",
                     role="ldo regulator"),
        SelectedPart(ref="C1", symbol="Device:C", value="22uF",
                     footprint="Package:C", role="output capacitor"),
    ])
    intent = NetlistIntent(
        nets=[
            NetIntent(name="5V", kind="power",
                      pins=[LogicalPin(ref="U1", pin="VI")]),
            NetIntent(name="3V3", kind="power",
                      pins=[LogicalPin(ref="U1", pin="VO"),
                            LogicalPin(ref="C1", pin="1")]),
            NetIntent(name="GND", kind="ground",
                      pins=[LogicalPin(ref="U1", pin="GND"),
                            LogicalPin(ref="C1", pin="2")]),
        ],
        supply_nets=["5V", "3V3"],
        ground_net="GND",
    )

    checks = _functional_connection_checks(selection, intent)
    failures = [
        c for c in checks
        if c.name.startswith("datasheet_connection") and not c.ok
    ]
    assert not failures, f"compliant design flagged: {[c.message for c in failures]}"
    passing = next(c for c in checks if c.name == "datasheet_connection")
    assert passing.ok
