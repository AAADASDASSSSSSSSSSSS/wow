"""Tests for the AHE inner-loop evidence and falsification machinery.

Grounded in the real kicad-cli ERC schema observed in a live run: violations
carry a rule ``type`` plus ``items`` whose descriptions name the object, e.g.
``"Symbol U1 Pin 7 [NRST, Input, Line]"``.
"""

import json
from pathlib import Path

from agents.ratsnestpro.evidence import (
    ViolationDigest,
    ViolationFinding,
    compare_signatures,
    digest_from_pipeline_result,
    distill_reports,
    parse_drc_report,
    parse_erc_report,
)
from agents.ratsnestpro.repair import (
    RepairAction,
    RepairPatch,
    RepairPreconditions,
    evaluate_change,
)

ERC_REPORT = {
    "coordinate_units": "mm",
    "sheets": [
        {
            "path": "/",
            "violations": [
                {
                    "type": "pin_not_connected",
                    "severity": "error",
                    "description": "Pin not connected",
                    "items": [
                        {"description": "Symbol U1 Pin 7 [NRST, Input, Line]"},
                    ],
                },
                {
                    "type": "pin_not_connected",
                    "severity": "error",
                    "description": "Pin not connected",
                    "items": [
                        {"description": "Symbol U1 Pin 44 [BOOT0, Input, Line]"},
                    ],
                },
                {
                    "type": "power_pin_not_driven",
                    "severity": "error",
                    "description": "Power pin not driven",
                    "items": [
                        {"description": "Symbol U1 Pin 1 [VBAT, Power input, Line]"},
                    ],
                },
                {
                    "type": "pin_not_connected",
                    "severity": "error",
                    "description": "Pin not connected",
                    "items": [{"description": "Symbol J1 Pin A5 [CC1, Passive, Line]"}],
                },
                {
                    "type": "label_dangling",
                    "severity": "warning",
                    "description": "Dangling label",
                    "items": [{"description": "Label 'GND'"}],
                },
                {
                    "type": "footprint_link_issues",
                    "severity": "warning",
                    "description": "Footprint link issue",
                    "items": [{"description": "Symbol C3 [C]"}],
                },
            ],
        }
    ],
}

DRC_REPORT = {
    "violations": [
        {
            "type": "clearance",
            "severity": "error",
            "description": "Clearance violation",
            "items": [{"description": "Footprint U1 pad 7"}, {"description": "Net 'VBUS'"}],
        }
    ],
    "unconnected_items": [
        {
            "type": "unconnected_items",
            "severity": "error",
            "description": "Unconnected items",
            "items": [{"description": "Footprint J1 pad A5"}],
        }
    ],
}


def _write(tmp_path: Path, name: str, payload: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --- G1 evidence distillation ------------------------------------------------


def test_erc_report_parses_to_object_level_findings(tmp_path: Path) -> None:
    findings = parse_erc_report(_write(tmp_path, "b.erc.json", ERC_REPORT))

    assert len(findings) == 6
    first = findings[0]
    assert first.ref == "U1"
    assert first.pin == "7"
    assert first.pin_name == "NRST"
    assert first.electrical_type == "Input"
    assert first.signature == "pin_not_connected:U1:7"


def test_label_and_symbol_only_items_are_still_identified(tmp_path: Path) -> None:
    findings = parse_erc_report(_write(tmp_path, "b.erc.json", ERC_REPORT))
    by_rule = {f.rule_type: f for f in findings}

    assert by_rule["label_dangling"].label == "GND"
    assert by_rule["label_dangling"].object_key == "label:GND"
    assert by_rule["footprint_link_issues"].ref == "C3"


def test_drc_report_parses_pads_and_nets(tmp_path: Path) -> None:
    findings = parse_drc_report(_write(tmp_path, "b.drc.json", DRC_REPORT))

    refs = {f.ref for f in findings if f.ref}
    assert {"U1", "J1"} <= refs
    assert any(f.net == "VBUS" for f in findings)
    assert all(f.kind == "drc" for f in findings)


def test_digest_separates_errors_from_warnings(tmp_path: Path) -> None:
    digest = distill_reports(_write(tmp_path, "b.erc.json", ERC_REPORT))

    assert len(digest.errors) == 4
    assert len(digest.warnings) == 2
    # Warnings must not inflate the set a repair is asked to clear.
    assert all(":" in sig for sig in digest.error_signatures)
    assert "label_dangling:label:GND" not in digest.error_signatures


def test_digest_groups_by_object_worst_first(tmp_path: Path) -> None:
    digest = distill_reports(_write(tmp_path, "b.erc.json", ERC_REPORT))
    objects = digest.by_object()

    assert objects[0].ref == "U1"
    assert objects[0].errors == 3
    assert "7(NRST)" in objects[0].pins
    assert digest.target_refs() == ["U1", "J1"]


def test_prompt_digest_names_objects_pins_and_raw_report(tmp_path: Path) -> None:
    path = _write(tmp_path, "board.erc.json", ERC_REPORT)
    text = distill_reports(path).to_prompt()

    # Aggregated counts, then the actual objects, then the drill-down path.
    assert "4 error(s)" in text
    assert "pin_not_connected: 3" in text
    assert "U1" in text and "7(NRST)" in text and "44(BOOT0)" in text
    assert "J1" in text and "A5(CC1)" in text
    assert "board.erc.json" in text


def test_empty_or_missing_report_yields_empty_digest(tmp_path: Path) -> None:
    assert distill_reports(str(tmp_path / "nope.json")).findings == []
    assert distill_reports().to_prompt() == ""


def test_digest_from_pipeline_result_follows_report_paths(tmp_path: Path) -> None:
    erc_path = _write(tmp_path, "b.erc.json", ERC_REPORT)
    digest = digest_from_pipeline_result(
        {"verification": {"erc": {"report_path": str(erc_path)}, "drc": {}}}
    )

    assert len(digest.errors) == 4


# --- object-level flip comparison -------------------------------------------


def test_compare_signatures_reports_fixed_introduced_and_persisted() -> None:
    flips = compare_signatures(
        ["pin_not_connected:U1:7", "pin_not_connected:U1:44"],
        ["pin_not_connected:U1:44", "pin_not_connected:J1:A5"],
    )

    assert flips["fixed"] == ["pin_not_connected:U1:7"]
    assert flips["introduced"] == ["pin_not_connected:J1:A5"]
    assert flips["persisted"] == ["pin_not_connected:U1:44"]


# --- G2 predicted impact and verdicts ---------------------------------------


def _patch(predicted: list[str], risks: list[str] | None = None) -> RepairPatch:
    return RepairPatch(
        repair_scope="schematic_connections",
        preconditions=RepairPreconditions(),
        actions=[RepairAction(type="fix_connectivity", target="U1")],
        predicted_fixes=predicted,
        risk_objects=risks or [],
    )


def test_verdict_effective_when_every_prediction_holds() -> None:
    patch = _patch(["pin_not_connected:U1:7", "pin_not_connected:U1:44"])
    evaluation = evaluate_change(
        patch,
        {"fixed": ["pin_not_connected:U1:7", "pin_not_connected:U1:44"], "introduced": []},
    )

    assert evaluation.verdict == "EFFECTIVE"
    assert evaluation.hit_rate == "2/2"
    assert evaluation.keeps_result and evaluation.should_continue


def test_verdict_partially_effective_when_some_remain() -> None:
    patch = _patch(["pin_not_connected:U1:7", "pin_not_connected:U1:44"])
    evaluation = evaluate_change(patch, {"fixed": ["pin_not_connected:U1:7"], "introduced": []})

    assert evaluation.verdict == "PARTIALLY_EFFECTIVE"
    assert evaluation.still_failed == ["pin_not_connected:U1:44"]
    assert evaluation.keeps_result


def test_verdict_ineffective_when_nothing_changed() -> None:
    evaluation = evaluate_change(
        _patch(["pin_not_connected:U1:7"]), {"fixed": [], "introduced": []}
    )

    assert evaluation.verdict == "INEFFECTIVE"
    assert not evaluation.keeps_result
    assert not evaluation.should_continue


def test_verdict_harmful_reproduces_the_observed_degradation() -> None:
    # Real run: nets were deleted, so predicted fixes did not land and new
    # unconnected pins appeared. That must be HARMFUL, not merely "worse score".
    patch = _patch(["pin_not_connected:U1:7"])
    evaluation = evaluate_change(
        patch,
        {
            "fixed": [],
            "introduced": [
                "pin_not_connected:U2:1",
                "pin_not_connected:U2:2",
                "pin_not_connected:R5:1",
            ],
        },
    )

    assert evaluation.verdict == "HARMFUL"
    assert not evaluation.keeps_result
    assert evaluation.unattributed_regressions  # nobody predicted these
    assert len(evaluation.introduced) == 3


def test_verdict_mixed_when_fixes_and_regressions_coexist() -> None:
    patch = _patch(
        ["pin_not_connected:U1:7", "pin_not_connected:U1:44"],
        risks=["pin_not_connected:J1:A5"],
    )
    evaluation = evaluate_change(
        patch,
        {
            "fixed": ["pin_not_connected:U1:7", "pin_not_connected:U1:44"],
            "introduced": ["pin_not_connected:J1:A5"],
        },
    )

    assert evaluation.verdict == "MIXED"
    assert evaluation.risk_realized == ["pin_not_connected:J1:A5"]
    assert evaluation.unattributed_regressions == []
    assert evaluation.keeps_result


def test_unpredicted_fixes_still_count_as_progress() -> None:
    evaluation = evaluate_change(
        _patch([]),
        {"fixed": ["pin_not_connected:U1:7"], "introduced": []},
    )

    assert evaluation.unpredicted_fixes == ["pin_not_connected:U1:7"]
    assert evaluation.verdict == "PARTIALLY_EFFECTIVE"


def test_missing_patch_is_scored_without_crashing() -> None:
    evaluation = evaluate_change(None, {"fixed": [], "introduced": ["x:1"]})

    assert evaluation.verdict == "HARMFUL"


# --- G3 typed actions mutate the design state -------------------------------


def test_replace_symbol_action_removes_the_part_from_the_checkpoint() -> None:
    from agents.ratsnestpro.tools import _apply_selection_actions

    payload = {
        "intermediate_artifacts": {
            "selection": {
                "parts": [
                    {"ref": "U1", "symbol": "MCU:X", "value": "STM32F103C8T6"},
                    {"ref": "J6", "symbol": "Conn:Bad", "value": "USB"},
                ],
                "rationale": "original",
            }
        }
    }

    updated = _apply_selection_actions(payload, ["J6"])
    refs = [p["ref"] for p in updated["intermediate_artifacts"]["selection"]["parts"]]

    assert refs == ["U1"]
    # Unrelated parts and the rest of the checkpoint survive.
    assert "U1" in refs


def test_selection_mutation_is_a_noop_without_targets() -> None:
    from agents.ratsnestpro.tools import _apply_selection_actions

    payload = {"intermediate_artifacts": {"selection": {"parts": [], "rationale": "x"}}}

    assert _apply_selection_actions(payload, []) == payload
    assert _apply_selection_actions({"steps": []}, ["U1"]) == {"steps": []}


def test_digest_prompt_is_bounded_for_large_reports() -> None:
    findings = [
        ViolationFinding(
            kind="erc",
            rule_type="pin_not_connected",
            severity="error",
            description="Pin not connected",
            ref=f"U{i}",
            pin=str(i),
            item_description=f"Symbol U{i} Pin {i} [P, Input, Line]",
        )
        for i in range(200)
    ]
    text = ViolationDigest(findings=findings).to_prompt(max_objects=5)

    assert "200 error(s)" in text
    assert "and 195 more object(s)" in text
    assert len(text) < 4_000
