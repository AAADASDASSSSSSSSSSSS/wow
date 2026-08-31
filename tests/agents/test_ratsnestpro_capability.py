"""Phase 2 tests: hardware capability resolution and the AHE repair loop.

Covers docs/Intent_Routing_and_AHE_EHE.md sections 2.3, 4.3-4.8 and the SAME54
industrial edge gateway assertions #2 (fixed MCU resolves to ATSAME54P20A-AU and
is never polluted), #3 (an unavailable catalogue only lowers procurement
evidence) and #4 (a missing support network triggers structured completion
instead of ending the flow).
"""

import pytest
from langchain_core.messages import HumanMessage

from agents.ratsnestpro.capability import (
    ComponentConstraint,
    ConstraintSet,
    acquire_symbol,
    build_component_constraints,
    capability_is_implemented_by,
    evaluate_coverage,
    expand_obligations,
    normalize_order_code,
    order_code_base,
    order_code_family,
    order_code_matches,
    package_pin_count,
    required_capabilities,
    resolve_parts,
    resolve_primary_mcu,
    strip_runtime_evidence,
)
from agents.ratsnestpro.diagnosis import (
    FailureDiagnoser,
    classify_failure,
    is_derived_blocker,
)
from agents.ratsnestpro.ratsnestpro_agent import (
    _after_hardware,
    _component_queries,
    _primary_mcu_mention,
    initialize,
)
from agents.ratsnestpro.repair import (
    PIPELINE_ORDER,
    RepairPreconditions,
    plan_repair,
    resume_plan,
)

SAME54_MCU_CLAUSE = (
    "主 MCU 固定为 ATSAME54P20A-AU。使用真实 KiCad 符号,使用兼容的 TQFP-128 封装。\n"
    "不得替换为其他 SAME、SAM、STM32、ESP32、RP2040 或 ATmega。"
)
SAME54_FULL = (
    "This is a new PCB design-and-build request. Route this request to build mode.\n"
    "There is no existing KiCad project to review.\n"
    "请从需求开始设计一块新的工业以太网与 CAN-FD 数据采集网关。\n"
    "project_name:\nsame54-industrial-edge-gateway\n"
    "run_name:\nsame54-industrial-edge-gateway-e2e-v2\n"
    f"{SAME54_MCU_CLAUSE}\n"
    "提供一路 CAN-FD 接口。使用 SAME54 的 RMII Ethernet MAC 并自主选择真实 RMII PHY,使用 RJ45。\n"
    "microSD 使用 SDHC/SDIO 4-bit 模式。QSPI NOR Flash 容量至少 128 Mbit。\n"
    "USB-C 工作在 USB 2.0 Full-Speed Device 模式。提供两路 0-10 V 工业模拟输入。\n"
    "输入包括 9-36 V 工业直流输入。\n"
)


# --- 2.1a open-world part resolution (replaces the brand whitelist) ----------


def test_same54_order_code_resolves_without_a_brand_whitelist() -> None:
    resolved = resolve_primary_mcu(SAME54_MCU_CLAUSE)

    assert resolved is not None
    assert resolved.token == "ATSAME54P20A-AU"
    assert resolved.role == "mcu"
    assert resolved.package == "TQFP-128"
    assert resolved.substitution == "forbidden"
    assert "user_constraint" in resolved.sources


def test_unpinned_mcu_families_remain_selection_candidates() -> None:
    requirement = (
        "STM32F405RGT6 or ESP32-C3 are both acceptable; choose from Wi-Fi, BLE, "
        "low-power and cost requirements."
    )

    assert resolve_primary_mcu(requirement) is None
    assert ConstraintSet.from_requirement(requirement).mcu is None
    assert _component_queries(requirement) == []


def test_direct_use_of_one_mcu_is_an_exact_constraint() -> None:
    resolved = resolve_primary_mcu("Use RP2040 for the development board.")

    assert resolved is not None
    assert resolved.token == "RP2040"
    assert resolved.substitution == "forbidden"


def test_agent_primary_mcu_helper_resolves_previously_missed_family() -> None:
    # The old _MCU_RE whitelist returned "" for every ATSAME/SAME order code.
    assert _primary_mcu_mention(SAME54_MCU_CLAUSE) == "ATSAME54P20A-AU"


@pytest.mark.parametrize(
    "requirement",
    [
        "主控必须是 GD32F303CCT6。",
        "The MCU must be LPC1768 on this board.",
        "主控必须是 MSP430F5529。",
        "主控必须是 EFM32GG990F1024。",
        "主控必须是 R7FA6M4AF3CFB。",
        "主控必须是 PIC32MZ2048EFH100。",
    ],
)
def test_families_absent_from_the_old_whitelist_now_resolve(requirement: str) -> None:
    resolved = resolve_primary_mcu(requirement)

    assert resolved is not None
    assert resolved.role == "mcu"
    assert "user_constraint" in resolved.sources


def test_negated_alternatives_are_not_resolved_as_parts() -> None:
    tokens = [part.token.upper() for part in resolve_parts(SAME54_FULL)]

    assert "ATSAME54P20A-AU" in tokens
    for forbidden in ("RP2040", "STM32F405RGT6", "ATMEGA328P"):
        assert forbidden not in tokens


def test_project_and_run_name_slugs_are_not_mistaken_for_part_numbers() -> None:
    tokens = [part.token for part in resolve_parts(SAME54_FULL)]

    assert not any(token.startswith("same54-industrial") for token in tokens)


def test_runtime_appended_evidence_cannot_redefine_the_fixed_mcu() -> None:
    polluted = (
        f"{SAME54_MCU_CLAUSE}\n\n"
        "GROUNDED ARCHITECT EVIDENCE — use this evidence:\n"
        '{"symbol": {"lib_id": "MCU_ST_STM32F4:STM32F405RGTx"}}'
    )

    assert "STM32F405" not in strip_runtime_evidence(polluted)
    assert resolve_primary_mcu(polluted).token == "ATSAME54P20A-AU"


def test_package_and_interface_tokens_are_not_parts() -> None:
    tokens = [part.token.upper() for part in resolve_parts(SAME54_FULL)]

    for noise in ("TQFP-128", "USB-C", "RJ45", "SDIO", "RMII", "CAN-FD"):
        assert noise not in tokens


def test_component_queries_lead_with_the_fixed_mcu() -> None:
    queries = _component_queries(SAME54_FULL)

    assert queries[0] == "ATSAME54P20A-AU"


def test_order_code_helpers() -> None:
    assert normalize_order_code("ATSAME54P20A-AU") == "atsame54p20aau"
    # Vendor tray/tape suffix dropped; library names omit it.
    assert order_code_base("ATSAME54P20A-AU") == "atsame54p20a"
    assert order_code_family("atsame54p20aau") == "atsame54"
    # KiCad 'x' placeholder behaves as a wildcard.
    assert order_code_matches("STM32F405RGTx", "STM32F405RGT6")
    assert not order_code_matches("ATSAME54P20A", "ATSAME54N19A")
    assert package_pin_count("TQFP-128") == 128
    assert package_pin_count("") is None


# --- 2.2 structured component constraints -----------------------------------


def test_constraints_are_built_once_and_round_trip_through_state() -> None:
    constraints = ConstraintSet.from_requirement(SAME54_FULL)
    mcu = constraints.mcu

    assert mcu is not None
    assert mcu.manufacturer_part_number == "ATSAME54P20A-AU"
    assert mcu.substitution == "forbidden"
    assert mcu.package == "TQFP-128"

    restored = ConstraintSet.from_state(constraints.to_state())
    assert restored.mcu is not None
    assert restored.mcu.manufacturer_part_number == "ATSAME54P20A-AU"
    assert [c.role for c in restored.fixed] == ["mcu"]


def test_forbidden_constraint_rejects_a_family_neighbour() -> None:
    constraint = build_component_constraints(SAME54_MCU_CLAUSE)[0]

    assert constraint.allows("ATSAME54P20A-A")  # same die, vendor suffix only
    assert not constraint.allows("ATSAME54N19A-A")
    assert not constraint.allows("STM32F405RGT6")


def test_family_equivalent_policy_allows_same_family_only() -> None:
    constraint = ComponentConstraint(
        role="mcu",
        manufacturer_part_number="ATSAME54P20A-AU",
        substitution="family_equivalent",
    )

    assert constraint.allows("ATSAME54N19A-A")
    assert not constraint.allows("ATSAME51J20A-A")


# --- 2.5a Symbol Acquisition ladder -----------------------------------------


def test_wrong_pin_count_neighbour_is_rejected_not_substituted() -> None:
    constraint = ComponentConstraint(
        role="mcu",
        manufacturer_part_number="ATSAME54P20A-AU",
        substitution="forbidden",
        package="TQFP-128",
    )

    acquisition = acquire_symbol(constraint)

    assert not acquisition.resolved
    assert acquisition.tier == "unavailable"
    assert acquisition.failure_class == "symbol_mismatch"
    rejected_ids = [item.lib_id for item in acquisition.rejected]
    assert any("ATSAME54N19A-A" in lib_id for lib_id in rejected_ids)
    assert acquisition.next_actions


def test_installed_exact_match_is_accepted_at_the_first_rung() -> None:
    constraint = ComponentConstraint(
        role="mcu",
        manufacturer_part_number="ATSAME54N19A-A",
        substitution="forbidden",
        package="TQFP-100",
    )

    acquisition = acquire_symbol(constraint)

    assert acquisition.resolved
    assert acquisition.tier == "installed_exact"
    assert acquisition.lib_id == "MCU_Microchip_SAME:ATSAME54N19A-A"
    assert acquisition.pin_count == 100


def test_unknown_part_is_unavailable_with_next_actions() -> None:
    constraint = ComponentConstraint(
        role="mcu",
        manufacturer_part_number="ZZQQ9999XYZ-NOPE",
        substitution="forbidden",
    )

    acquisition = acquire_symbol(constraint)

    assert not acquisition.resolved
    assert acquisition.failure_class == "symbol_unavailable"


# --- 2.5b obligation graph + typed capability coverage ----------------------


def test_rj45_cannot_satisfy_the_rmii_phy_capability() -> None:
    assert not capability_is_implemented_by("rmii_phy", "rj45_magjack")
    assert capability_is_implemented_by("rmii_phy", "ethernet_phy")
    assert capability_is_implemented_by("ethernet_connector_with_magnetics", "rj45_magjack")


def test_required_capabilities_read_from_the_requirement() -> None:
    capabilities = required_capabilities(SAME54_FULL)

    for expected in (
        "rmii_phy",
        "can_fd",
        "sdio_4bit",
        "qspi_flash",
        "usb2_device",
        "analog_input_0_10v",
        "industrial_dc_input",
        "mcu_core",
    ):
        assert expected in capabilities


def test_buck_selection_expands_into_its_support_obligations() -> None:
    roles = {obligation.role for obligation in expand_obligations(["buck_regulator"])}

    assert {
        "buck_input_capacitor",
        "buck_output_capacitor",
        "buck_inductor",
        "buck_bootstrap_capacitor",
        "buck_feedback_network",
        "buck_compensation_network",
    } <= roles


def test_sdio_obligations_report_only_the_absent_pullups() -> None:
    report = evaluate_coverage(
        ["sdio_4bit"],
        ["microsd"],
        present_roles=["microsd", "cmd_pullup", "dat0_pullup"],
    )

    missing = {obligation.role for obligation in report.missing_obligations}
    assert "cmd_pullup" not in missing
    assert {"dat1_pullup", "dat2_pullup", "dat3_pullup"} <= missing
    assert report.failure_class == "missing_support_network"
    assert not report.complete


def test_coverage_is_complete_when_every_obligation_is_present() -> None:
    report = evaluate_coverage(
        ["ldo_regulator"],
        ["ldo_regulator"],
        present_roles=["ldo_regulator", "ldo_input_capacitor", "ldo_output_capacitor"],
    )

    assert report.complete
    assert report.failure_class == ""


def test_missing_capability_is_reported_as_missing_component() -> None:
    report = evaluate_coverage(["rmii_phy"], ["rj45_magjack"])

    assert report.missing_capabilities == ("rmii_phy",)
    assert report.failure_class == "missing_component"


# --- 2.3 FailureDiagnoser ---------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected_class", "expected_strategy"),
    [
        (
            "pin_pad_compatibility:J6: 16 symbol pins do not match 11 pads",
            "footprint_mismatch",
            "acquire_symbol",
        ),
        (
            "support_network:U2: missing buck input capacitor and inductor",
            "missing_support_network",
            "extend_selection",
        ),
        (
            "topology: capability rmii_phy is not implemented by any part",
            "missing_component",
            "extend_selection",
        ),
        (
            "component pin U1:42 belongs to both SDIO_CLK and QSPI_SCK",
            "pin_conflict",
            "reassign_pins",
        ),
        (
            "symbol MCU_Microchip_SAME:ATSAME54P20A is not found",
            "symbol_unavailable",
            "acquire_symbol",
        ),
        (
            "order code ATSAME54N19A-A does not satisfy ATSAME54P20A-AU",
            "symbol_mismatch",
            "acquire_symbol",
        ),
        (
            "kicad-cli DRC unavailable",
            "tool_unavailable",
            "record_capability_gap",
        ),
        (
            "datasheet fetch timed out after 30s",
            "transient_external_failure",
            "retry_with_alternate_source",
        ),
        (
            "freerouting did not complete: 37 unrouted connections",
            "routing_congestion",
            "adjust_layout_or_routing",
        ),
        (
            "track width 0.10mm is below minimum 0.127mm",
            "manufacturing_violation",
            "fix_flagged_object",
        ),
        (
            "selection rejected: substitution=forbidden for the requested MCU",
            "constraint_violation",
            "block_honestly",
        ),
        (
            "TypeError: unexpected keyword argument 'foo'",
            "harness_defect",
            "record_capability_gap",
        ),
    ],
)
def test_failure_taxonomy_maps_to_the_documented_strategy(
    message: str, expected_class: str, expected_strategy: str
) -> None:
    diagnosis = classify_failure(message)

    assert diagnosis.failure_class == expected_class
    assert diagnosis.strategy == expected_strategy


def test_constraint_violation_is_never_auto_repaired() -> None:
    report = FailureDiagnoser().diagnose_messages(
        ["selection rejected: substitution=forbidden for the requested MCU"]
    )

    assert not report.should_attempt_repair
    assert plan_repair(report, preconditions=RepairPreconditions()) is None


def test_derived_blockers_do_not_create_spurious_capability_gaps() -> None:
    assert is_derived_blocker("17-step pipeline did not complete")
    assert is_derived_blocker("no actual .kicad_sch artifact")
    assert not is_derived_blocker("kicad-cli DRC unavailable")

    report = FailureDiagnoser().diagnose_pipeline_result(
        {
            "status": "blocked",
            "steps": [],
            "release_blockers": [
                "17-step pipeline did not complete",
                "no actual .kicad_pcb artifact",
            ],
        }
    )
    assert report.diagnoses == []


def test_pipeline_result_diagnosis_picks_the_earliest_scope() -> None:
    report = FailureDiagnoser().diagnose_pipeline_result(
        {
            "status": "blocked",
            "completed_steps": 3,
            "steps": [
                {
                    "name": "selection",
                    "blocked": True,
                    "summary": "70 parts",
                    "failed_checks": [
                        {
                            "name": "support_network:U2",
                            "message": "missing buck input capacitor and inductor",
                        }
                    ],
                }
            ],
            "release_blockers": ["kicad-cli DRC unavailable"],
        }
    )

    assert report.should_attempt_repair
    assert report.primary_scope() == "selection"
    assert [d.failure_class for d in report.capability_gaps] == ["tool_unavailable"]


# --- 2.4 structured incremental repair patches ------------------------------


def test_missing_support_network_produces_a_scoped_selection_patch() -> None:
    report = FailureDiagnoser().diagnose_messages(
        ["support_network:U2: missing buck input capacitor and inductor"],
        step="selection",
    )

    patch = plan_repair(
        report,
        preconditions=RepairPreconditions(selection_version=3, completed_steps=3),
        missing_roles=["buck_input_capacitor", "buck_inductor"],
    )

    assert patch is not None
    assert patch.repair_scope == "selection"
    action = patch.actions[0]
    assert action.type == "add_support_network"
    assert action.target == "U2"
    assert action.roles == ["buck_input_capacitor", "buck_inductor"]


def test_patch_reruns_only_the_scope_and_downstream_steps() -> None:
    report = FailureDiagnoser().diagnose_messages(
        ["component pin U1:42 belongs to both SDIO_CLK and QSPI_SCK"],
        step="schematic_pinmap",
    )
    patch = plan_repair(report, preconditions=RepairPreconditions())

    assert patch is not None
    assert patch.repair_scope == "schematic_pinmap"
    assert patch.affected_steps[0] == "schematic_pinmap"
    assert "selection" not in patch.affected_steps
    assert patch.affected_steps[-1] == PIPELINE_ORDER[-1]

    plan = resume_plan(patch, ["requirements", "topology", "selection", "schematic_pinmap"])
    assert plan["resume_from"] == "schematic_pinmap"
    assert plan["keep_steps"] == ["requirements", "topology", "selection"]
    assert plan["discard_steps"] == ["schematic_pinmap"]


def test_stale_patch_is_not_applicable_after_state_moves_on() -> None:
    report = FailureDiagnoser().diagnose_messages(
        ["support_network:U2: missing decoupling capacitor"], step="selection"
    )
    patch = plan_repair(
        report, preconditions=RepairPreconditions(state_version=1, selection_version=3)
    )

    assert patch is not None
    assert patch.is_applicable(RepairPreconditions(state_version=1, selection_version=3))
    assert not patch.is_applicable(RepairPreconditions(state_version=1, selection_version=4))
    assert not patch.is_applicable(RepairPreconditions(state_version=2, selection_version=3))


def test_repair_action_authority_is_bounded_by_the_strategy() -> None:
    report = FailureDiagnoser().diagnose_messages(
        ["component pin U1:42 belongs to both SDIO_CLK and QSPI_SCK"],
        step="schematic_pinmap",
    )
    patch = plan_repair(report, preconditions=RepairPreconditions())

    assert patch is not None
    # A pin conflict may only reassign pins; it must not rewrite the BOM.
    assert {action.type for action in patch.actions} == {"reassign_pins"}


# --- graph integration ------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_stores_constraints_and_capabilities_once() -> None:
    state = await initialize(
        {"messages": [HumanMessage(content=SAME54_FULL)]},
        {"configurable": {"thread_id": "same54"}},
    )

    assert state["workflow_mode"] == "build"
    assert state["capability"]["primary_mcu"] == "ATSAME54P20A-AU"
    assert state["capability"]["primary_mcu_package"] == "TQFP-128"
    assert state["project_name"] == "same54-industrial-edge-gateway"
    mcu = ConstraintSet.from_state(state["component_constraints"]).mcu
    assert mcu is not None
    assert mcu.manufacturer_part_number == "ATSAME54P20A-AU"
    assert mcu.substitution == "forbidden"


@pytest.mark.asyncio
async def test_initialize_marks_requirement_driven_mcu_selection() -> None:
    state = await initialize(
        {
            "messages": [HumanMessage(content=(
                "Design a new battery-powered controller with Wi-Fi, BLE, two UARTs, "
                "twelve GPIOs and OTA. Select the MCU from these capabilities."
            ))]
        },
        {"configurable": {"thread_id": "capability-selection"}},
    )

    assert state["capability"]["selection_mode"] == "capability_only"
    assert state["capability"]["primary_mcu"] == ""
    assert ConstraintSet.from_state(state["component_constraints"]).mcu is None


@pytest.mark.asyncio
async def test_initialize_keeps_broad_family_for_later_selection() -> None:
    requirement = "主控使用 STM32，至少两个 UART；具体型号由选型阶段决定。"

    state = await initialize(
        {"messages": [HumanMessage(content=requirement)]},
        {"configurable": {"thread_id": "family-selection"}},
    )

    assert resolve_primary_mcu(requirement) is None
    assert ConstraintSet.from_state(state["component_constraints"]).mcu is None
    assert state["capability"]["selection_mode"] == "capability_only"
    assert "mcu_family_any_of=STM32" in state["capability"]["required_capabilities"]
    assert _component_queries(requirement, state["component_constraints"]) == []


def test_recoverable_failure_routes_to_in_task_repair() -> None:
    assert (
        _after_hardware(
            {
                "hardware": {"release_ready": False, "project_available": True},
                "diagnosis": {"should_attempt_repair": True},
                "repair_patches": [{"patch": {}}],
                "review_round": 0,
                "max_review_rounds": 2,
            }
        )
        == "hardware_repair_phase"
    )


def test_unrecoverable_failure_does_not_loop_on_repair() -> None:
    assert (
        _after_hardware(
            {
                "hardware": {"release_ready": False, "project_available": True},
                "diagnosis": {"should_attempt_repair": False},
                "repair_patches": [],
                "review_round": 0,
                "max_review_rounds": 2,
            }
        )
        == "reviewer_phase"
    )


def test_repair_rounds_are_bounded() -> None:
    assert (
        _after_hardware(
            {
                "hardware": {"release_ready": False, "project_available": True},
                "diagnosis": {"should_attempt_repair": True},
                "repair_patches": [{"patch": {}}],
                "review_round": 2,
                "max_review_rounds": 2,
            }
        )
        == "reviewer_phase"
    )
