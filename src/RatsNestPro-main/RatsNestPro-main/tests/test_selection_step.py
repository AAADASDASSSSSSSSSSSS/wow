"""Task 5: selection step + selection bottom-line check (real-library grounding)."""

from __future__ import annotations

import json

import pytest

from ratsnestpro import config
from ratsnestpro.agents.llm import LlmMode
from ratsnestpro.eda import symbols
from ratsnestpro.orchestration.pipeline import (
    Pipeline,
    PipelineContext,
    PipelineState,
    PipelineStep,
    RequirementsStep,
    SelectionStep,
    Severity,
    TopologyStep,
    _apply_selection_identity_policy,
    _close_truncated_json,
    _component_symbol_hints,
    _fixed_mcu_models,
    _ground_selected_parts,
    _grounded_vcap_uf,
    _library_voltage_rating_v,
    _mcu_capability_requirements,
    _mcu_family_options,
    _mcu_models,
    _normalize_footprint_for_symbol,
    _normalize_grounded_values,
    _normalize_symbol_for_footprint,
    _requested_mcu_symbols,
    _required_input_rating_v,
    _selection_requirement_checks,
    _specific_component_identity_error,
    _symbol_power_pin_counts,
    _uncovered_topology_blocks,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    ComponentRoleSpec,
    SelectedPart,
    SelectionPlan,
    TopologyBlock,
    TopologyPlan,
)


class FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        return self._responses.pop(0) if self._responses else "{}"


def test_truncated_json_after_complete_array_item_is_closed() -> None:
    truncated = (
        '{"parts":[{"ref":"R1","symbol":"Device:R","value":"10k",'
        '"footprint":"MyLib:Part","role":"resistor"}'
    )

    repaired = _close_truncated_json(truncated)
    plan = SelectionPlan.model_validate_json(repaired)

    assert [part.ref for part in plan.parts] == ["R1"]


def test_selection_warns_on_excessive_single_pass_part_count(monkeypatch) -> None:
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    plan = SelectionPlan(
        parts=[
            SelectedPart(
                ref=f"R{index}",
                symbol="Device:R",
                value="10k",
                role="resistor",
            )
            for index in range(1, 130)
        ]
    )

    checks = SelectionStep().check(PipelineState(requirement_text="resistors"), plan)

    compact = next(check for check in checks if check.name == "compact_part_count")
    assert not compact.ok
    assert compact.severity == Severity.WARNING


def test_selection_accepts_complex_board_with_seventy_parts(monkeypatch) -> None:
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    plan = SelectionPlan(
        parts=[
            SelectedPart(
                ref=f"R{index}",
                symbol="Device:R",
                value="10k",
                role="resistor",
            )
            for index in range(1, 71)
        ]
    )

    checks = SelectionStep().check(PipelineState(requirement_text="controller"), plan)

    compact = next(check for check in checks if check.name == "compact_part_count")
    assert compact.ok


def test_selection_repair_applies_bounded_delta_without_rewriting_parts(
    monkeypatch,
) -> None:
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    initial = json.dumps({
        "parts": [
            {
                "ref": "R1",
                "symbol": "Device:R",
                "value": "120",
                "role": "can_termination_resistor",
            },
            {
                "ref": "U1",
                "symbol": "Interface_CAN_LIN:SN65HVD230",
                "value": "SN65HVD230",
                "role": "can_transceiver",
            },
        ],
    })
    repair_patch = json.dumps({
        "upsert_parts": [
            {
                "ref": "JP1",
                "symbol": "Jumper:Jumper_2_Open",
                "value": "CAN TERM ENABLE",
                "role": "can_termination_jumper",
            },
        ],
        "remove_refs": [],
        "rationale": "add the missing selectable termination jumper",
    })
    state = PipelineState(
        requirement_text="CAN 120 ohm selectable jumper termination"
    )

    result = SelectionStep().run(
        state,
        PipelineContext(
            mode=LlmMode.AUTO,
            client=FakeLLM([initial, repair_patch]),
            repair_attempts=1,
        ),
    )

    assert not result.blocked
    plan = state.artifacts[PipelineStep.SELECTION]
    assert isinstance(plan, SelectionPlan)
    assert [part.ref for part in plan.parts] == ["R1", "U1", "JP1"]
    assert next(part for part in plan.parts if part.ref == "U1").role == (
        "can_transceiver"
    )


def test_selectable_can_termination_requires_resistor_and_selector() -> None:
    requirement = "CAN 120 ohm selectable jumper termination"
    resistor_only = [
        SelectedPart(
            ref="R1",
            symbol="Device:R",
            value="120",
            role="can_termination_resistor",
        ),
    ]

    missing = next(
        check
        for check in _selection_requirement_checks(requirement, resistor_only)
        if check.name == "can_selectable_termination_parts"
    )
    complete = next(
        check
        for check in _selection_requirement_checks(
            requirement,
            [
                *resistor_only,
                SelectedPart(
                    ref="JP1",
                    symbol="Jumper:Jumper_2_Open",
                    value="Jumper",
                    role="can_termination_jumper",
                ),
            ],
        )
        if check.name == "can_selectable_termination_parts"
    )

    assert not missing.ok
    assert complete.ok


def test_switching_regulator_support_is_derived_from_real_symbol_pins(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        symbols,
        "symbol_pins",
        lambda _lib_id: [
            {"number": "1", "name": "BOOT"},
            {"number": "2", "name": "VIN"},
            {"number": "3", "name": "RT/CLK"},
            {"number": "4", "name": "GND"},
            {"number": "5", "name": "SW"},
            {"number": "6", "name": "COMP"},
            {"number": "7", "name": "FB"},
        ],
    )
    converter = SelectedPart(
        ref="U2",
        symbol="Test:Buck",
        value="Buck",
        role="buck_regulator",
    )
    essentials = [
        SelectedPart(ref="C1", symbol="Device:C", value="1u", role="buck_input_capacitor"),
        SelectedPart(ref="C2", symbol="Device:C", value="10u", role="buck_output_capacitor"),
        SelectedPart(ref="L1", symbol="Device:L", value="10u", role="buck_inductor"),
    ]
    requirement = "7-24V switching power supply"

    missing = next(
        check
        for check in _selection_requirement_checks(
            requirement,
            [converter, *essentials],
        )
        if check.name == "switching_regulator_support_parts:U2"
    )
    complete = next(
        check
        for check in _selection_requirement_checks(
            requirement,
            [
                converter,
                *essentials,
                SelectedPart(
                    ref="C3",
                    symbol="Device:C",
                    value="100n",
                    role="buck_bootstrap_capacitor",
                ),
                SelectedPart(
                    ref="R1",
                    symbol="Device:R",
                    value="100k",
                    role="buck_feedback_resistor_high",
                ),
                SelectedPart(
                    ref="R2",
                    symbol="Device:R",
                    value="10k",
                    role="buck_feedback_resistor_low",
                ),
                SelectedPart(
                    ref="R3",
                    symbol="Device:R",
                    value="100k",
                    role="buck_timing_resistor",
                ),
                SelectedPart(ref="R4", symbol="Device:R", value="10k", role="buck_comp_resistor"),
                SelectedPart(ref="C4", symbol="Device:C", value="10n", role="buck_comp_capacitor"),
            ],
        )
        if check.name == "switching_regulator_support_parts:U2"
    )

    assert not missing.ok
    assert "bootstrap capacitor" in missing.message
    assert complete.ok


def test_two_external_analog_channels_require_a_real_connector(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        symbols,
        "symbol_pins",
        lambda _lib_id: [
            {"number": "1", "name": "AI1"},
            {"number": "2", "name": "AI2"},
            {"number": "3", "name": "GND"},
        ],
    )
    protection = [
        SelectedPart(
            ref=f"D{channel}",
            symbol="Device:D_TVS",
            value="TVS",
            role=f"analog_input_overvoltage_protection_{channel}",
        )
        for channel in (1, 2)
    ]
    requirement = "two external 0–10 V analog inputs"

    missing = next(
        check
        for check in _selection_requirement_checks(requirement, protection)
        if check.name == "analog_input_external_connector"
    )
    complete = next(
        check
        for check in _selection_requirement_checks(
            requirement,
            [
                *protection,
                SelectedPart(
                    ref="J1",
                    symbol="Connector_Generic:Conn_01x03",
                    value="Analog input",
                    role="analog_input_connector",
                ),
            ],
        )
        if check.name == "analog_input_external_connector"
    )

    assert not missing.ok
    assert complete.ok


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    symbols._load_lib_node.cache_clear()
    # Don't auto-load any project .env during tests.
    monkeypatch.setattr(config, "_ENV_LOADED", True)
    yield
    symbols._load_lib_node.cache_clear()


def _fixture_libs(tmp_path, monkeypatch):
    """A tiny symbol .kicad_symdir (Device:R) + footprint .pretty (MyLib:Part)."""
    symdir = tmp_path / "Device.kicad_symdir"
    symdir.mkdir()
    (symdir / "R.kicad_sym").write_text(
        '(kicad_symbol_lib (version 20231120) (generator "t")'
        '(symbol "R" (symbol "R_1_1" '
        '(pin passive line (at 0 3.81 270) (length 2.54) (name "~") (number "1"))'
        '(pin passive line (at 0 -3.81 90) (length 2.54) (name "~") (number "2")))))',
        encoding="utf-8",
    )
    pretty = tmp_path / "MyLib.pretty"
    pretty.mkdir()
    (pretty / "Part.kicad_mod").write_text(
        '(footprint "Part" (layer "F.Cu")'
        '(pad "1" smd rect (at -0.8 0) (size 1 1) (layers "F.Cu"))'
        '(pad "2" smd rect (at 0.8 0) (size 1 1) (layers "F.Cu")))',
        encoding="utf-8",
    )
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(tmp_path))
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(tmp_path))


def test_offline_selection_yields_no_bom_instead_of_another_board(monkeypatch) -> None:
    # With no proposal there is no BOM. This used to return the hard-coded
    # ATmega328 reference BOM, so a failed model call on any requirement
    # silently produced an ATmega dev board that passed every later check.
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    state = PipelineState(requirement_text="ATmega328 USB-C 3.3V 8MHz dev board")
    Pipeline().run(state, PipelineContext(mode=LlmMode.OFFLINE), until=PipelineStep.SELECTION)
    sel = state.artifact(PipelineStep.SELECTION)
    assert isinstance(sel, SelectionPlan)
    assert sel.parts == []
    # Uncovered topology blocks are an ERROR again, so the empty BOM blocks
    # rather than being waved through as a deterministic-fallback warning.
    assert state.blocked
    covered = next(
        c for c in state.results[-1].checks if c.name == "topology_blocks_covered"
    )
    assert not covered.ok and covered.severity is Severity.ERROR


def test_grounding_does_not_scan_fallback_libraries_when_unconfigured(
    monkeypatch,
) -> None:
    # "Unconfigured" now means unconfigured AND undiscoverable: config.symbol_dir
    # falls back to any KiCad install it can find, so deleting the env var alone
    # no longer implies the libraries are absent. Stubbing discovery keeps this
    # guard about wasted work rather than about the test host's KiCad.
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    monkeypatch.setattr(config, "_first_discovered", lambda kind: None)
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.grounding.ground_symbol",
        lambda _lib_id: pytest.fail("unconfigured symbol grounding was called"),
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.grounding.ground_footprint",
        lambda _lib_id: pytest.fail("unconfigured footprint grounding was called"),
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline._ground_mpns",
        lambda _parts: None,
    )
    part = SelectedPart(
        ref="R1",
        symbol="Device:R",
        value="10k",
        footprint="Resistor_SMD:R_0603_1608Metric",
        role="resistor",
    )

    _ground_selected_parts([part], "one resistor")

    assert part.symbol == "Device:R"
    assert part.footprint == "Resistor_SMD:R_0603_1608Metric"


def test_offline_selection_does_not_build_unused_llm_library_hints(
    monkeypatch,
) -> None:
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline._requested_mcu_symbols",
        lambda _requirement: pytest.fail("unused MCU hints were built"),
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline._component_symbol_hints",
        lambda _requirement: pytest.fail("unused component hints were built"),
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline._ground_mpns",
        lambda _parts: None,
    )

    plan, used_llm = SelectionStep().propose(
        PipelineState(requirement_text="ATmega328 USB-C board"),
        PipelineContext(mode=LlmMode.OFFLINE),
        knowledge="",
    )

    assert isinstance(plan, SelectionPlan)
    assert not used_llm


def test_selection_verifies_against_real_libraries(tmp_path, monkeypatch) -> None:
    _fixture_libs(tmp_path, monkeypatch)
    good = json.dumps(
        {
            "parts": [
                {"ref": "R1", "symbol": "Device:R", "value": "10k",
                 "footprint": "MyLib:Part", "role": "resistor"}
            ],
            "rationale": "ok",
        }
    )
    fake = FakeLLM([good])
    state = PipelineState(requirement_text="one resistor")
    SelectionStep().run(state, PipelineContext(mode=LlmMode.AUTO, client=fake))
    result = state.results[-1]
    assert not result.blocked
    assert all(c.ok for c in result.checks)


def test_selection_missing_symbol_blocks(tmp_path, monkeypatch) -> None:
    _fixture_libs(tmp_path, monkeypatch)
    bad = json.dumps(
        {
            "parts": [
                {"ref": "U9", "symbol": "Device:DoesNotExist", "value": "x",
                 "footprint": "MyLib:Part", "role": "mcu"}
            ]
        }
    )
    fake = FakeLLM([bad])
    state = PipelineState(requirement_text="bogus part")
    SelectionStep().run(state, PipelineContext(mode=LlmMode.AUTO, client=fake))
    result = state.results[-1]
    assert result.blocked
    assert any(c.name == "symbol:U9" and not c.ok for c in result.checks)


def test_selection_missing_footprint_blocks(tmp_path, monkeypatch) -> None:
    _fixture_libs(tmp_path, monkeypatch)
    bad = json.dumps(
        {
            "parts": [
                {"ref": "R1", "symbol": "Device:R", "value": "10k",
                 "footprint": "MyLib:Ghost", "role": "resistor"}
            ]
        }
    )
    fake = FakeLLM([bad])
    state = PipelineState(requirement_text="bad footprint")
    SelectionStep().run(state, PipelineContext(mode=LlmMode.AUTO, client=fake))
    result = state.results[-1]
    assert result.blocked
    assert any(c.name == "footprint:R1" and not c.ok for c in result.checks)


def test_selection_blocks_symbol_footprint_pin_mismatch(tmp_path, monkeypatch) -> None:
    _fixture_libs(tmp_path, monkeypatch)
    mismatched = json.dumps(
        {
            "parts": [
                {
                    "ref": "R1",
                    "symbol": "Device:R",
                    "value": "10k",
                    "footprint": "MyLib:Part",
                    "role": "resistor",
                }
            ]
        }
    )
    fake = FakeLLM([mismatched])
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.footprints.footprint_pads",
        lambda _lib_id: [
            {"number": "1"},
            {"number": "2"},
            {"number": "3"},
        ],
    )
    state = PipelineState(requirement_text="one resistor")

    SelectionStep().run(state, PipelineContext(mode=LlmMode.AUTO, client=fake))

    result = state.results[-1]
    compatibility = next(
        c for c in result.checks if c.name == "pin_pad_compatibility:R1"
    )
    assert not compatibility.ok


def test_normalize_pin_header_symbol_from_footprint(monkeypatch) -> None:
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.footprints.footprint_pads",
        lambda _lib_id: [{"number": str(number)} for number in range(1, 11)],
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: (
            [{"number": f"a{number}"} for number in range(1, 6)]
            + [{"number": f"b{number}"} for number in range(1, 6)]
            if "DIN41612" in lib_id
            else [{"number": str(number)} for number in range(1, 11)]
        ),
    )
    part = SelectedPart(
        ref="J2",
        symbol="Connector:DIN41612_02x05_AB_EvenPins",
        value="SWD",
        footprint=(
            "Connector_PinHeader_2.54mm:"
            "PinHeader_2x05_P2.54mm_Vertical"
        ),
        role="swd",
    )

    assert _normalize_symbol_for_footprint(part) == (
        "Connector_Generic:Conn_02x05_Odd_Even"
    )


def test_normalize_single_row_connector_symbol_from_header(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.footprints.footprint_pads",
        lambda _lib_id: [
            {"number": str(number)}
            for number in range(1, 5)
        ],
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: (
            [
                {"number": str(number)}
                for number in range(1, 5)
            ]
            if lib_id == "Connector_Generic:Conn_01x04"
            else [
                {"number": "1"},
                {"number": "2"},
                {"number": "3"},
                {"number": "4"},
                {"number": "SH"},
            ]
        ),
    )
    part = SelectedPart(
        ref="J3",
        symbol="Connector:Shielded_Connector",
        value="Header",
        footprint=(
            "Connector_PinHeader_2.54mm:"
            "PinHeader_1x04_P2.54mm_Vertical"
        ),
        role="expansion_header",
    )

    assert _normalize_symbol_for_footprint(part) == (
        "Connector_Generic:Conn_01x04"
    )


def test_normalize_generic_connector_footprint_from_symbol(monkeypatch) -> None:
    expected = (
        "Connector_PinHeader_2.54mm:"
        "PinHeader_2x08_P2.54mm_Vertical"
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda _lib_id: [{"number": str(number)} for number in range(1, 17)],
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.footprints.footprint_pads",
        lambda lib_id: [
            {"number": str(number)}
            for number in range(1, 17 if lib_id == expected else 12)
        ],
    )
    part = SelectedPart(
        ref="J6",
        symbol="Connector_Generic:Conn_02x08_Odd_Even",
        value="EXPANSION",
        footprint="Connector_FFC-FPC:Conn_01x11",
        role="connector",
    )

    assert _normalize_footprint_for_symbol(part) == expected


def test_normalize_generic_connector_footprint_from_shorthand_alias(
    monkeypatch,
) -> None:
    expected = (
        "Connector_PinHeader_2.54mm:"
        "PinHeader_2x08_P2.54mm_Vertical"
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda _lib_id: None,
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.footprints.footprint_pads",
        lambda lib_id: [
            {"number": str(number)}
            for number in range(1, 17 if lib_id == expected else 12)
        ],
    )
    part = SelectedPart(
        ref="J6",
        symbol="Connector_Generic:Conn_02x08",
        value="EXPANSION",
        footprint="Connector_FFC-FPC:Conn_01x11",
        role="connector",
    )

    assert _normalize_footprint_for_symbol(part) == expected


def test_normalize_can_common_mode_choke_to_grounded_four_pad_footprint(
    monkeypatch,
) -> None:
    expected = "Inductor_SMD:L_CommonModeChoke_Coilcraft_1812CAN"
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda _lib_id: [
            {"number": "1"},
            {"number": "2"},
            {"number": "3"},
            {"number": "4"},
        ],
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.footprints.footprint_pads",
        lambda lib_id: (
            [{"number": str(number)} for number in range(1, 5)]
            if lib_id == expected
            else [{"number": "1"}, {"number": "2"}]
        ),
    )
    part = SelectedPart(
        ref="L1",
        symbol="Device:L_Coupled",
        value="CAN common-mode choke",
        footprint="Inductor_SMD:L_10.4x10.4_H4.8",
        role="can_common_mode_choke",
    )

    assert _normalize_footprint_for_symbol(part) == expected


def test_normalize_single_dip_switch_symbol_from_footprint(monkeypatch) -> None:
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.footprints.footprint_pads",
        lambda _lib_id: [{"number": "1"}, {"number": "2"}],
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: (
            [{"number": "1"}, {"number": "2"}]
            if lib_id == "Switch:SW_DIP_x01"
            else [
                {"number": "1"},
                {"number": "2"},
                {"number": "3"},
                {"number": "4"},
            ]
        ),
    )
    part = SelectedPart(
        ref="SW4",
        symbol="Switch:SW_DIP_x02",
        value="CAN_Term",
        footprint=(
            "Button_Switch_THT:"
            "SW_DIP_SPSTx01_Slide_9.78x4.72mm_W7.62mm_P2.54mm"
        ),
        role="can_term_switch",
    )

    assert _normalize_symbol_for_footprint(part) == "Switch:SW_DIP_x01"


def test_selection_blocks_mcu_family_substitution(monkeypatch) -> None:
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    state = PipelineState(requirement_text="RP2040 USB-C development board")
    plan = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="MCU_Microchip_ATmega:ATmega328P-A",
            value="ATmega328P-AU",
            footprint="Package_QFP:TQFP-32_7x7mm_P0.8mm",
            role="mcu",
        )
    ])

    checks = SelectionStep().check(state, plan)

    identity = next(c for c in checks if c.name == "requested_mcu_selected")
    assert not identity.ok


def test_selection_ignores_negated_mcu_and_still_blocks_substitution(
    monkeypatch,
) -> None:
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    state = PipelineState(
        requirement_text="Use RP2040, not ATmega328P. Never substitute ATmega128."
    )
    plan = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="MCU_Microchip_ATmega:ATmega128-16A",
            value="ATmega128-16A",
            footprint="Package_QFP:TQFP-64_14x14mm_P0.8mm",
            role="mcu",
        )
    ])

    checks = SelectionStep().check(state, plan)

    assert _mcu_models(state.requirement_text) == {"rp2040"}
    identity = next(c for c in checks if c.name == "requested_mcu_selected")
    assert not identity.ok


def test_mcu_model_parser_requires_specific_atmega_model() -> None:
    assert _mcu_models("Use RP2040; never substitute ATmega.") == {"rp2040"}


def test_mcu_alternatives_are_candidates_not_fixed_identities() -> None:
    requirement = (
        "STM32F405RGT6 或 ESP32-C3 都可以，请根据 Wi-Fi、BLE、低功耗和成本选择。"
    )

    assert _mcu_models(requirement) == {"stm32f405rgt6", "esp32c3"}
    assert _fixed_mcu_models(requirement) == set()


def test_explicit_mcu_constraint_remains_fixed() -> None:
    assert _fixed_mcu_models("主控必须是 STM32F405RGT6，禁止替换。") == {
        "stm32f405rgt6"
    }


def test_mcu_capabilities_are_extracted_without_selecting_a_family() -> None:
    capabilities = _mcu_capability_requirements(
        "需要 Wi-Fi、BLE、2 路 UART、12 路 GPIO、至少 4 MB Flash，电池低功耗供电。"
    )

    assert {
        "mcu_core",
        "wifi",
        "bluetooth_le",
        "uart>=2",
        "gpio>=12",
        "flash>=4 MB",
        "low_power",
    } <= set(capabilities)


def test_topology_clears_unpinned_mcu_identity_from_llm() -> None:
    fake = FakeLLM([
        json.dumps({
            "blocks": [{"name": "mcu", "kind": "mcu"}],
            "component_roles": [{
                "role": "mcu",
                "value": "ESP32-C3",
                "symbol": "RF_Module:ESP32-C3-WROOM-02",
                "footprint": "RF_Module:ESP32-C3-WROOM-02",
                "manufacturer": "Espressif",
                "exact_mpn": "ESP32-C3-WROOM-02",
            }],
            "rails": ["3V3"],
            "ground_net": "GND",
        })
    ])

    plan, used_llm = TopologyStep().propose(
        PipelineState(
            requirement_text="需要 Wi-Fi、BLE、2 路 UART 和低功耗，由系统选择主控。"
        ),
        PipelineContext(mode=LlmMode.AUTO, client=fake),
        knowledge="",
    )

    role = plan.component_roles[0]
    assert used_llm
    assert role.selection_mode == "capability_only"
    assert role.exact_mpn == role.manufacturer == role.symbol == role.footprint == ""
    assert {"wifi", "bluetooth_le", "uart>=2", "low_power"} <= set(
        role.required_capabilities
    )


def test_topology_preserves_only_user_fixed_mcu_identity() -> None:
    fake = FakeLLM([
        json.dumps({
            "blocks": [{"name": "mcu", "kind": "mcu"}],
            "component_roles": [{"role": "mcu", "exact_mpn": "ESP32-C3"}],
            "rails": ["3V3"],
            "ground_net": "GND",
        })
    ])

    plan, _used_llm = TopologyStep().propose(
        PipelineState(requirement_text="主控必须是 STM32F405RGT6，禁止替换。"),
        PipelineContext(mode=LlmMode.AUTO, client=fake),
        knowledge="",
    )

    role = plan.component_roles[0]
    assert role.selection_mode == "fixed_exact"
    assert role.exact_mpn == "STM32F405RGT6"


def test_broad_mcu_family_is_a_selection_option_not_an_exact_mpn() -> None:
    assert _fixed_mcu_models("主控使用 STM32，由系统选择具体型号。") == set()
    assert _mcu_family_options("STM32 或 ESP32 都可以。") == ["STM32", "ESP32"]
    assert "mcu_family_any_of=STM32|ESP32" in _mcu_capability_requirements(
        "STM32 或 ESP32 都可以，要求低功耗。"
    )


def test_topology_keeps_broad_family_in_capabilities_only() -> None:
    fake = FakeLLM([
        json.dumps({
            "blocks": [{"name": "mcu", "kind": "mcu"}],
            "component_roles": [{"role": "mcu", "exact_mpn": "STM32"}],
            "rails": ["3V3"],
            "ground_net": "GND",
        })
    ])

    plan, _used_llm = TopologyStep().propose(
        PipelineState(requirement_text="主控使用 STM32，由系统选择具体型号。"),
        PipelineContext(mode=LlmMode.AUTO, client=fake),
        knowledge="",
    )

    role = plan.component_roles[0]
    assert role.selection_mode == "capability_only"
    assert role.exact_mpn == ""
    assert "mcu_family_any_of=STM32" in role.required_capabilities


def test_selection_does_not_require_every_optional_mcu_family(monkeypatch) -> None:
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    state = PipelineState(
        requirement_text="STM32F405RGT6 或 ESP32-C3 都可以，根据功能选择一个。"
    )
    plan = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="RF_Module:ESP32-C3-WROOM-02",
            value="ESP32-C3-WROOM-02",
            role="mcu",
        )
    ])

    checks = SelectionStep().check(state, plan)

    assert all(check.name != "requested_mcu_selected" for check in checks)


def test_family_metadata_is_attached_only_after_capability_selection() -> None:
    topology = TopologyPlan(
        blocks=[TopologyBlock(name="mcu", kind="mcu")],
        component_roles=[
            ComponentRoleSpec(
                role="mcu",
                selection_mode="capability_only",
                required_capabilities=["wifi", "bluetooth_le", "low_power"],
            )
        ],
        rails=["3V3"],
    )
    plan = SelectionPlan(
        parts=[
            SelectedPart(
                ref="U1",
                symbol="RF_Module:ESP32-C3-WROOM-02",
                value="ESP32-C3-WROOM-02",
                role="mcu",
            )
        ],
        rationale="best grounded capability match",
    )

    _apply_selection_identity_policy(plan, "需要 Wi-Fi、BLE 和低功耗。", topology)

    part = plan.parts[0]
    assert part.identity_mode == "capability_only"
    assert part.device_family == "ESP32"
    assert part.validation_profile == "ESP32-C3"


def test_family_option_is_stamped_as_variant_after_selection() -> None:
    plan = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="RF_Module:ESP32-C3-WROOM-02",
            value="ESP32-C3-WROOM-02",
            role="mcu",
        )
    ])

    _apply_selection_identity_policy(plan, "STM32 或 ESP32 都可以。", None)

    assert plan.parts[0].identity_mode == "family_variant"
    assert plan.parts[0].requested_identity == "STM32 | ESP32"


def test_family_constraint_is_checked_after_selection() -> None:
    state = PipelineState(requirement_text="主控使用 STM32，由系统选择具体型号。")
    wrong = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="RF_Module:ESP32-C3-WROOM-02",
            value="ESP32-C3-WROOM-02",
            role="mcu",
        )
    ])
    right = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="MCU_ST_STM32F4:STM32F405RGT6",
            value="STM32F405RGT6",
            role="mcu",
        )
    ])

    wrong_check = next(
        check for check in SelectionStep().check(state, wrong)
        if check.name == "requested_mcu_family_selected"
    )
    right_check = next(
        check for check in SelectionStep().check(state, right)
        if check.name == "requested_mcu_family_selected"
    )

    assert not wrong_check.ok
    assert right_check.ok


def test_mcu_model_parser_ignores_run_and_project_names() -> None:
    requirement = (
        "Design STM32F405RGT6. "
        "run_name: stm32-gated-e2e project_name: stm32f405-gated"
    )

    assert _mcu_models(requirement) == {"stm32f405rgt6"}


def test_mcu_model_parser_ignores_long_chinese_forbidden_list() -> None:
    requirement = (
        "主控必须是 STM32F405RGT6，LQFP-64 封装，"
        "禁止替换为其他 STM32、RP2040、ESP32 或 ATmega。"
    )

    assert _mcu_models(requirement) == {"stm32f405rgt6"}


def test_mcu_model_parser_ignores_review_artifact_paths() -> None:
    requirement = (
        "Repair STM32F405RGT6 using report "
        "/data/runs/stm32-full-chain-v5/stm32f405-full-chain-v5.erc.json"
    )

    assert _mcu_models(requirement) == {"stm32f405rgt6"}


def test_mcu_model_parser_ignores_grounded_architect_evidence() -> None:
    requirement = (
        "Use STM32F405RGT6.\n\n"
        "GROUNDED ARCHITECT EVIDENCE — "
        '{"candidates":["STM32F405RGTx","STM32F405ZGTx","STM32F415RGTx"]}'
    )

    assert _mcu_models(requirement) == {"stm32f405rgt6"}


def test_mcu_model_parser_uses_installed_kicad_mcu_families(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.grounding.symbol_index",
        lambda: ("MCU_GigaDevice:GD32F303CCTx", "Device:R"),
    )

    assert _mcu_models("Use GD32F303CCT6.") == {"gd32f303cct6"}
    assert _mcu_models("Do not use GD32F303CCT6; use RP2040.") == {"rp2040"}


def test_power_pin_count_supports_non_stm32_supply_names(monkeypatch) -> None:
    monkeypatch.setattr(
        symbols,
        "symbol_pins",
        lambda _lib_id: [
            {"name": "IOVDD"},
            {"name": "DVDD"},
            {"name": "USB_VDD"},
            {"name": "ADC_AVDD"},
            {"name": "VBAT"},
            {"name": "VCAP_1"},
            {"name": "GND"},
        ],
    )

    assert _symbol_power_pin_counts("MCU_Test:Part") == {
        "VDD": 3,
        "VDDA": 1,
        "VBAT": 1,
        "VCAP": 1,
    }


def test_topology_coverage_detects_unimplemented_unfamiliar_block() -> None:
    state = PipelineState(requirement_text="x")
    state.artifacts[PipelineStep.TOPOLOGY] = TopologyPlan(blocks=[
        TopologyBlock(
            name="Ethernet PHY",
            kind="communications",
            description="RMII Ethernet physical layer",
        ),
        TopologyBlock(
            name="Motor Driver",
            kind="actuator",
            description="H-bridge motor output stage",
        ),
    ])
    parts = [
        SelectedPart(
            ref="U2",
            symbol="Interface_Ethernet:LAN8720A",
            value="LAN8720A",
            footprint="Package_QFP:LQFP-48_7x7mm_P0.5mm",
            role="ethernet_phy",
        ),
    ]

    assert _uncovered_topology_blocks(state, parts) == ["Motor Driver"]


def test_grounded_vcap_value_normalizes_selected_capacitors() -> None:
    requirement = (
        "Design a controller.\n\n"
        "GROUNDED ARCHITECT EVIDENCE — "
        "CEXT Capacitance of external capacitor 2.2 μF"
    )
    parts = [
        SelectedPart(
            ref="C5",
            symbol="Device:C",
            value="4.7u",
            role="mcu_vcap_1",
        ),
        SelectedPart(
            ref="C6",
            symbol="Device:C",
            value="4.7u",
            role="mcu_vcap_2",
        ),
    ]
    plan = SelectionPlan(parts=parts)

    assert _grounded_vcap_uf(requirement) == 2.2
    before = next(
        check
        for check in SelectionStep().check(
            PipelineState(requirement_text=requirement),
            plan,
        )
        if check.name == "grounded_vcap_capacitance"
    )
    assert not before.ok
    _normalize_grounded_values(parts, requirement)
    assert [part.value for part in parts] == ["2.2uF", "2.2uF"]
    after = next(
        check
        for check in SelectionStep().check(
            PipelineState(requirement_text=requirement),
            plan,
        )
        if check.name == "grounded_vcap_capacitance"
    )
    assert after.ok


def test_selection_accepts_requested_mcu_regardless_of_role_label(monkeypatch) -> None:
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    state = PipelineState(requirement_text="Design an RP2040 controller")
    plan = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="MCU_RaspberryPi:RP2040",
            value="RP2040",
            footprint="Package_DFN_QFN:QFN-56-1EP_7x7mm_P0.4mm_EP3.2x3.2mm",
            role="microcontroller",
        )
    ])

    checks = SelectionStep().check(state, plan)

    identity = next(c for c in checks if c.name == "requested_mcu_selected")
    assert identity.ok


def test_requested_mcu_symbols_uses_installed_library_index(monkeypatch) -> None:
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.grounding.symbol_index",
        lambda: (
            "MCU_Microchip_ATmega:ATmega328P-A",
            "MCU_RaspberryPi:RP2040",
            "MCU_ST_STM32F4:STM32F405RGT",
        ),
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_properties",
        lambda lib_id: {
            "Footprint": "Package_DFN_QFN:QFN-56-1EP_7x7mm_P0.4mm_EP3.2x3.2mm"
        } if lib_id.endswith(":RP2040") else {},
    )

    assert _requested_mcu_symbols("Use an RP2040 MCU") == [
        {
            "symbol": "MCU_RaspberryPi:RP2040",
            "footprint": (
                "Package_DFN_QFN:"
                "QFN-56-1EP_7x7mm_P0.4mm_EP3.2x3.2mm"
            ),
        }
    ]


def test_requested_mcu_symbol_matches_kicad_order_code_wildcard(monkeypatch) -> None:
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.grounding.symbol_index",
        lambda: ("MCU_ST_STM32F4:STM32F405RGTx",),
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_properties",
        lambda _lib_id: {"Footprint": "Package_QFP:LQFP-64_10x10mm_P0.5mm"},
    )

    assert _requested_mcu_symbols("Use STM32F405RGT6") == [
        {
            "symbol": "MCU_ST_STM32F4:STM32F405RGTx",
            "footprint": "Package_QFP:LQFP-64_10x10mm_P0.5mm",
        }
    ]


def test_selection_blocks_wrong_requested_mcu_footprint(monkeypatch) -> None:
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_properties",
        lambda _lib_id: {
            "Footprint": "Package_DFN_QFN:QFN-56-1EP_7x7mm_P0.4mm_EP3.2x3.2mm"
        },
    )
    state = PipelineState(requirement_text="Design an RP2040 controller")
    plan = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="MCU_RaspberryPi:RP2040",
            value="RP2040",
            footprint="Package_QFP:LQFP-64_7x7mm_P0.4mm",
            role="mcu",
        )
    ])

    checks = SelectionStep().check(state, plan)

    package = next(c for c in checks if c.name == "mcu_footprint:U1")
    assert not package.ok


@pytest.mark.parametrize(
    ("part", "properties", "message"),
    [
        (
            SelectedPart(
                ref="U3",
                symbol="Memory_Flash:W25Q16JVSS",
                value="W25Q64JVSS",
                role="spi_flash",
            ),
            {
                "Value": "W25Q16JVSS",
                "Description": "16Mbit Serial Flash Memory",
            },
            "different device",
        ),
        (
            SelectedPart(
                ref="U4",
                symbol="Interface_Optical:TSOP345xx",
                value="ADXL345",
                role="accelerometer",
            ),
            {
                "Value": "TSOP345xx",
                "Description": "IR Receiver Module",
            },
            "Sensor_Motion:",
        ),
        (
            SelectedPart(
                ref="U6",
                symbol="Regulator_Switching:TPS54233",
                value="TPS54331",
                role="dc_dc_5v",
            ),
            {
                "Value": "TPS54233",
                "Description": "28V step-down regulator",
            },
            "different device",
        ),
        (
            SelectedPart(
                ref="D8",
                symbol="LED:LED_Cree_XHP50_6V",
                value="LED_Green",
                role="power_led",
            ),
            {
                "Value": "LED_Cree_XHP50_6V",
                "Description": "XLamp XHP50 high-power LED",
            },
            "different device",
        ),
        (
            SelectedPart(
                ref="D1",
                symbol="Diode:MBR0520",
                value="MBR0520L",
                role="reverse_polarity_protection",
            ),
            {
                "Value": "MBR0520",
                "Description": "20V Schottky diode",
            },
            "different device",
        ),
    ],
)
def test_specific_component_identity_rejects_relabeling(
    monkeypatch,
    part,
    properties,
    message,
) -> None:
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_properties",
        lambda _lib_id: properties,
    )

    error = _specific_component_identity_error(
        part,
        "SPI Flash capacity at least 64 Mbit",
    )

    assert error is not None
    assert message in error


@pytest.mark.parametrize(
    ("part", "properties"),
    [
        (
            SelectedPart(
                ref="U3",
                symbol="Memory_Flash:W25Q128JVS",
                value="W25Q128JVS",
                role="spi_flash",
            ),
            {
                "Value": "W25Q128JVS",
                "Description": "128Mbit Serial Flash Memory",
            },
        ),
        (
            SelectedPart(
                ref="U4",
                symbol="Sensor_Motion:LIS3DH",
                value="LIS3DH",
                role="accelerometer",
            ),
            {
                "Value": "LIS3DH",
                "Description": "3-Axis Accelerometer, I2C/SPI",
            },
        ),
        (
            SelectedPart(
                ref="D8",
                symbol="Device:LED",
                value="LED_Green",
                role="power_led",
            ),
            {
                "Value": "LED",
                "Description": "Light emitting diode",
            },
        ),
    ],
)
def test_specific_component_identity_accepts_real_equivalents(
    monkeypatch,
    part,
    properties,
) -> None:
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_properties",
        lambda _lib_id: properties,
    )

    assert _specific_component_identity_error(
        part,
        "SPI Flash capacity at least 64 Mbit",
    ) is None


def test_specific_component_identity_does_not_misclassify_support_parts(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_properties",
        lambda _lib_id: {
            "Value": "C",
            "Description": "Unpolarized capacitor",
        },
    )
    part = SelectedPart(
        ref="C15",
        symbol="Device:C",
        value="10u",
        role="dc_dc_5v_output_cap",
    )

    assert _specific_component_identity_error(part, "7-24V input") is None


def test_complex_requirement_coverage_reports_missing_circuits() -> None:
    requirement = (
        "microSD SDIO 4-bit with ESD. CAN 共模 protection. "
        "两路 0–10 V 模拟输入 with overvoltage protection. "
        "外部直流输入优先，两个输入不得反向灌电。"
    )
    parts = [
        SelectedPart(
            ref="R14",
            symbol="Device:R",
            value="10k",
            role="sdio_cmd_pullup",
        ),
        SelectedPart(
            ref="R15",
            symbol="Device:R",
            value="10k",
            role="sdio_dat3_pullup",
        ),
    ]

    checks = _selection_requirement_checks(requirement, parts)

    failed = {check.name for check in checks if not check.ok}
    assert failed == {
        "microsd_sdio4_pullups",
        "microsd_esd_protection",
        "can_common_mode_protection",
        "analog_input_external_connector",
        "analog_input_overvoltage_protection",
        "dual_input_priority_and_backfeed",
    }


def test_complex_requirement_coverage_accepts_explicit_roles(monkeypatch) -> None:
    requirement = (
        "microSD SDIO 4-bit with ESD. CAN common-mode protection. "
        "two 0-10 V analog inputs with overvoltage protection. "
        "external-input priority and no backfeed."
    )
    references_and_roles = [
        ("R1", "sdio_cmd_pullup"),
        ("R2", "sdio_dat0_pullup"),
        ("R3", "sdio_dat1_pullup"),
        ("R4", "sdio_dat2_pullup"),
        ("R5", "sdio_dat3_pullup"),
        ("D1", "microsd_esd"),
        ("L1", "can_common_mode_choke"),
        ("D2", "analog_input_overvoltage_protection_1"),
        ("D3", "analog_input_overvoltage_protection_2"),
        ("J1", "analog_input_connector"),
        ("U1", "power_mux"),
    ]
    parts = []
    for reference, role in references_and_roles:
        if role == "can_common_mode_choke":
            symbol = "Device:L_Coupled"
        elif role == "analog_input_connector":
            symbol = "Connector_Generic:Conn_01x03"
        else:
            symbol = "Device:R"
        parts.append(
            SelectedPart(
                ref=reference,
                symbol=symbol,
                value="test",
                role=role,
            )
        )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: (
            [{"number": str(number)} for number in range(1, 5)]
            if lib_id == "Device:L_Coupled"
            else (
                [{"number": "1"}, {"number": "2"}, {"number": "3"}]
                if lib_id == "Connector_Generic:Conn_01x03"
                else [{"number": "1"}, {"number": "2"}]
            )
        ),
    )

    checks = _selection_requirement_checks(requirement, parts)

    assert checks
    assert all(check.ok for check in checks)


def test_complex_requirement_coverage_rejects_role_label_cheating() -> None:
    requirement = (
        "microSD SDIO 4-bit with ESD. CAN common-mode protection. "
        "two 0-10 V analog inputs with overvoltage protection. "
        "external-input priority and no backfeed."
    )
    parts = [
        SelectedPart(
            ref=f"R{index}",
            symbol="Device:R",
            value="10k",
            role=role,
        )
        for index, role in enumerate(
            (
                "sdio_cmd_pullup",
                "sdio_dat0_pullup",
                "sdio_dat1_pullup",
                "sdio_dat2_pullup",
                "sdio_dat3_pullup",
                "microsd_esd",
                "can_common_mode_choke",
                "analog_input_overvoltage_protection_1",
                "analog_input_overvoltage_protection_2",
                "power_mux",
            ),
            start=1,
        )
    ]

    checks = _selection_requirement_checks(requirement, parts)

    assert {
        check.name for check in checks if not check.ok
    } == {
        "microsd_esd_protection",
        "can_common_mode_protection",
        "analog_input_external_connector",
        "analog_input_overvoltage_protection",
        "dual_input_priority_and_backfeed",
    }


def test_industrial_input_rating_uses_surge_headroom(monkeypatch) -> None:
    part = SelectedPart(
        ref="D1",
        symbol="Diode:MBR0520",
        value="MBR0520",
        role="reverse_polarity_protection",
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_properties",
        lambda _lib_id: {
            "Description": "20V 0.5A Schottky Power Rectifier Diode"
        },
    )

    assert _required_input_rating_v(
        "7–24 V external input with surge protection"
    ) == 36
    assert _library_voltage_rating_v(part) == 20


def test_semantic_symbol_hints_offer_real_functional_equivalents(
    monkeypatch,
) -> None:
    installed = (
        "Sensor_Motion:LIS3DH",
        "Memory_Flash:W25Q128JVS",
        "Device:LED",
        "Regulator_Switching:TPS54360DDA",
        "Diode:SS34",
        "Power_Management:TPS2116DRL",
        "Connector:Micro_SD_Card",
        "Connector_Generic:Conn_01x03",
        "Connector_Generic:Conn_02x05_Odd_Even",
        "Device:L_Coupled",
        "Transistor_FET:AO3401A",
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.grounding.symbol_index",
        lambda: installed,
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: (
            [
                {"number": "1"},
                {"number": "2"},
                {"number": "3"},
                {"number": "4"},
            ]
            if lib_id == "Device:L_Coupled"
            else [{"number": "1"}, {"number": "2"}]
        ),
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_properties",
        lambda lib_id: {
            "Value": lib_id.partition(":")[2],
            "Description": "grounded candidate",
            "Datasheet": "https://example.invalid/datasheet",
            "Footprint": "Package:Real",
        },
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.footprints.footprint_pads",
        lambda lib_id: (
            [{"number": str(index)} for index in range(1, 5)]
            if lib_id == "Inductor_SMD:L_CommonModeChoke_Coilcraft_1812CAN"
            else None
        ),
    )

    hints = _component_symbol_hints(
        "7–24 V to 5 V, external-input priority and no backfeed; "
        "SPI NOR W25Q64, accelerometer, LED, microSD SDIO, CAN common-mode "
        "choke and standard interface with GND, standard 10-pin SWD"
    )

    assert hints["3-axis accelerometer"][0]["symbol"] == (
        "Sensor_Motion:LIS3DH"
    )
    assert hints["SPI NOR Flash (>=64 Mbit)"][0]["symbol"] == (
        "Memory_Flash:W25Q128JVS"
    )
    assert hints["7-24V industrial 5V buck"][0]["symbol"] == (
        "Regulator_Switching:TPS54360DDA"
    )
    assert hints["24V reverse-polarity diode"][0]["symbol"] == "Diode:SS34"
    assert hints["5V source-priority power mux"][0]["symbol"] == (
        "Power_Management:TPS2116DRL"
    )
    assert hints["microSD socket"][0]["symbol"] == "Connector:Micro_SD_Card"
    assert hints["CAN common-mode choke"][0]["symbol"] == "Device:L_Coupled"
    assert hints["CAN common-mode choke"][0]["compatible_footprints"] == [
        "Inductor_SMD:L_CommonModeChoke_Coilcraft_1812CAN"
    ]
    assert hints["CANH/CANL/GND connector"][0]["symbol"] == (
        "Connector_Generic:Conn_01x03"
    )
    assert hints["10-pin Cortex SWD connector"][0]["symbol"] == (
        "Connector_Generic:Conn_02x05_Odd_Even"
    )
    assert hints["5V reverse-blocking P-MOSFET"][0]["symbol"] == (
        "Transistor_FET:AO3401A"
    )


def test_selection_counts_mcu_decouplers_from_real_supply_pins(
    monkeypatch,
) -> None:
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: (
            [
                {"number": "19", "name": "VDD"},
                {"number": "32", "name": "VDD"},
                {"number": "48", "name": "VDD"},
                {"number": "64", "name": "VDD"},
                {"number": "13", "name": "VDDA"},
                {"number": "1", "name": "VBAT"},
                {"number": "31", "name": "VCAP_1"},
                {"number": "47", "name": "VCAP_2"},
            ]
            if "STM32F405" in lib_id
            else []
        ),
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_properties",
        lambda _lib_id: {},
    )
    parts = [
        SelectedPart(
            ref="U1",
            symbol="MCU_ST_STM32F4:STM32F405RGTx",
            value="STM32F405RGT6",
            role="mcu",
        ),
        *[
            SelectedPart(
                ref=f"C{index}",
                symbol="Device:C",
                value="100nF",
                role=f"mcu_vdd_decoupling_{index}",
            )
            for index in range(1, 6)
        ],
        SelectedPart(
            ref="C6",
            symbol="Device:C",
            value="100nF",
            role="mcu_vdda_decoupling",
        ),
    ]
    state = PipelineState(
        requirement_text=(
            "STM32F405RGT6；每个电源引脚附近独立的 100 nF 去耦"
        )
    )

    checks = SelectionStep().check(state, SelectionPlan(parts=parts))

    count = next(
        check for check in checks
        if check.name == "mcu_supply_decoupling_count"
    )
    assert not count.ok
    assert "VDD expected 4, found 5" in count.message
    assert "VDDA expected 1, found 1" in count.message


def test_generic_same_rail_decouplers_are_not_per_pin_claims(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda _lib_id: [
            {"number": "8", "name": "VDD"},
            {"number": "9", "name": "VSS"},
        ],
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_properties",
        lambda _lib_id: {},
    )
    parts = [
        SelectedPart(
            ref="U1",
            symbol="MCU_ST_STM32G0:STM32G070RBTx",
            value="STM32G070RBT6",
            role="mcu",
        ),
        *[
            SelectedPart(
                ref=f"C{index}",
                symbol="Device:C",
                value="100nF",
                role="mcu_vdd_decoupling",
            )
            for index in range(3, 7)
        ],
    ]
    state = PipelineState(
        requirement_text="STM32G070RBT6 with four shared-rail 100nF MCU decouplers"
    )

    checks = SelectionStep().check(state, SelectionPlan(parts=parts))

    upper_bound = next(
        check for check in checks
        if check.name == "mcu_supply_decoupling_not_excessive"
    )
    assert upper_bound.ok
    assert "VDD expected at most 1, found 0" in upper_bound.message


@pytest.mark.parametrize(
    ("requirement", "role", "pin_count", "expected"),
    [
        ("microSD socket", "microsd_connector", 2, 9),
        ("CAN interface with GND", "can_interface", 2, 3),
        ("standard 10-pin Cortex SWD", "swd_interface", 2, 10),
    ],
)
def test_selection_blocks_functionally_undersized_connectors(
    tmp_path,
    monkeypatch,
    requirement: str,
    role: str,
    pin_count: int,
    expected: int,
) -> None:
    _fixture_libs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda _lib_id: [
            {"number": str(number), "name": f"Pin_{number}"}
            for number in range(1, pin_count + 1)
        ],
    )
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.footprints.footprint_pads",
        lambda _lib_id: [
            {"number": str(number)}
            for number in range(1, pin_count + 1)
        ],
    )
    part = SelectedPart(
        ref="J1",
        symbol="Device:R",
        value="connector",
        footprint="MyLib:Part",
        role=role,
    )

    checks = SelectionStep().check(
        PipelineState(requirement_text=requirement),
        SelectionPlan(parts=[part]),
    )

    functional = next(
        check for check in checks
        if check.name == "functional_pin_count:J1"
    )
    assert not functional.ok
    assert f"at least {expected}" in functional.message


def test_microsd_esd_device_is_not_checked_as_a_socket(
    tmp_path,
    monkeypatch,
) -> None:
    _fixture_libs(tmp_path, monkeypatch)
    part = SelectedPart(
        ref="D8",
        symbol="Device:R",
        value="ESD",
        footprint="MyLib:Part",
        role="microsd_cmd_esd",
    )

    checks = SelectionStep().check(
        PipelineState(requirement_text="microSD socket with ESD"),
        SelectionPlan(parts=[part]),
    )

    assert not any(
        check.name == "functional_pin_count:D8"
        for check in checks
    )


def test_can_tvs_requires_two_real_protection_channels(monkeypatch) -> None:
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: (
            [{"number": "1"}, {"number": "2"}]
            if lib_id == "Device:D_TVS"
            else [
                {"number": "1"},
                {"number": "2"},
                {"number": "3"},
            ]
        ),
    )
    single = SelectedPart(
        ref="D1",
        symbol="Device:D_TVS",
        value="CAN TVS",
        role="can_bus_tvs",
    )
    requirement = "CANH/CANL must each have TVS surge protection"

    one_channel = _selection_requirement_checks(requirement, [single])
    one_check = next(
        check for check in one_channel
        if check.name == "can_differential_tvs_channels"
    )
    assert not one_check.ok

    dual = SelectedPart(
        ref="U9",
        symbol="Power_Protection:USBLC6-2SC6",
        value="dual TVS",
        role="can_bus_tvs",
    )
    two_channels = _selection_requirement_checks(requirement, [dual])
    two_check = next(
        check for check in two_channels
        if check.name == "can_differential_tvs_channels"
    )
    assert two_check.ok


def test_front_three_steps_run_in_order(tmp_path, monkeypatch) -> None:
    _fixture_libs(tmp_path, monkeypatch)
    state = PipelineState(requirement_text="ATmega328 dev board 3.3V 8MHz")
    # Offline fallback references real KiCad lib_ids not in the fixture, so run
    # only requirements+topology here; selection verified separately above.
    Pipeline([RequirementsStep(), TopologyStep()]).run(
        state, PipelineContext(mode=LlmMode.OFFLINE), until=PipelineStep.TOPOLOGY
    )
    assert state.completed == [PipelineStep.REQUIREMENTS, PipelineStep.TOPOLOGY]
