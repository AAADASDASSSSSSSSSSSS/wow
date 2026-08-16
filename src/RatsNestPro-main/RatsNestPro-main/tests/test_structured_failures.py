"""Structured failure information, deterministic corrections, and their traces.

Three pieces that only pay off together:

* a check declares its failure class and the objects it is about, instead of the
  diagnosis layer recovering both from the message prose;
* a step that corrects something deterministically records what it corrected;
* the in-step repair feedback carries the class and targets, plus the kind of
  edit that class needs.

The recording is the part that is easy to skip and expensive to skip. AHE
attributes a change in check outcomes to whatever it believes acted, so a silent
correction lands in ``unattributed_regressions``, which can read HARMFUL as
EFFECTIVE — and HARMFUL is what forces a rollback.
"""

from __future__ import annotations

import pytest

from ratsnestpro.orchestration.check_classes import (
    CHECK_FAILURE_CLASS,
    CLASS_REPAIR_DIRECTIVE,
    failure_class_for,
    repair_directives,
)
from ratsnestpro.orchestration.pipeline import (
    CheckResult,
    PipelineState,
    PipelineStep,
    StepResult,
    _normalize_bridged_capacitor_return,
    _normalize_duplicate_supply_pins,
    _repair_failure_line,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    LogicalPin,
    NetIntent,
    NetlistIntent,
    SelectedPart,
    SelectionPlan,
)

# --------------------------------------------------------------------------- #
# A check declares its class and its targets
# --------------------------------------------------------------------------- #


def test_failure_class_comes_from_the_table() -> None:
    check = CheckResult(name="supply_pin_not_on_regulator_input:U1", ok=False)
    assert check.failure_class == "erc_violation"
    assert check.failure_class == failure_class_for(check.name)


def test_unmapped_check_declares_no_class() -> None:
    """``None`` means "no declaration", which consumers must read as "infer"."""
    assert CheckResult(name="no_such_check", ok=False).failure_class is None


def test_targets_survive_serialisation() -> None:
    """The payload boundary is a dict, so the field has to cross it."""
    check = CheckResult(
        name="two_terminal_not_shorted:C10",
        ok=False,
        targets=["C10", "VDD33"],
    )
    assert check.model_dump(mode="json")["targets"] == ["C10", "VDD33"]


def test_targets_can_hold_what_the_message_regex_cannot_find() -> None:
    """Net names and two-letter designators are why this field exists.

    The downstream fallback matches ``[UJDRCLQYFK]\\d{1,3}`` in prose, so ``FB1``,
    ``MH1`` and every net name are invisible to it.
    """
    check = CheckResult(
        name="power_pin_rail_class",
        ok=False,
        targets=["FB1", "MH1", "/REG_IN", "U1:24"],
    )
    assert check.targets == ["FB1", "MH1", "/REG_IN", "U1:24"]


# --------------------------------------------------------------------------- #
# auto_fixes
# --------------------------------------------------------------------------- #


def test_step_result_carries_auto_fixes() -> None:
    result = StepResult(
        step=PipelineStep.SCH_CONNECTIONS,
        auto_fixes=["moved C10 pin 2 to GND"],
    )
    assert result.model_dump(mode="json")["auto_fixes"] == ["moved C10 pin 2 to GND"]


def test_state_records_fixes_per_step() -> None:
    state = PipelineState(requirement_text="x")
    state.record_auto_fix(PipelineStep.SCH_CONNECTIONS, "first")
    state.record_auto_fix(PipelineStep.SCH_CONNECTIONS, "second")
    state.record_auto_fix(PipelineStep.SELECTION, "elsewhere")
    assert state.auto_fixes[PipelineStep.SCH_CONNECTIONS] == ["first", "second"]
    assert state.auto_fixes[PipelineStep.SELECTION] == ["elsewhere"]


def test_a_discarded_round_can_be_truncated_back() -> None:
    """The mechanism ``run`` uses when a repair candidate loses.

    A correction made while building a candidate that is then thrown away did not
    ship, and recording it would attribute an effect to a change that is not in
    the artifact.
    """
    state = PipelineState(requirement_text="x")
    step = PipelineStep.SCH_CONNECTIONS
    state.record_auto_fix(step, "kept")
    before = len(state.auto_fixes.get(step, ()))
    state.record_auto_fix(step, "from the losing round")
    del state.auto_fixes.setdefault(step, [])[before:]
    assert state.auto_fixes[step] == ["kept"]


# --------------------------------------------------------------------------- #
# The one deterministic correction: a bridged two-pin capacitor
# --------------------------------------------------------------------------- #


def _selection() -> SelectionPlan:
    return SelectionPlan(parts=[
        SelectedPart(ref="C10", symbol="Device:C", value="100nF", role=""),
        SelectedPart(ref="C1", symbol="Device:C", value="10uF", role=""),
        SelectedPart(
            ref="U1",
            symbol="MCU_ST_STM32F1:STM32F103C8Tx",
            value="STM32F103C8Tx",
            role="",
        ),
    ])


def _bridged_plan() -> NetlistIntent:
    return NetlistIntent(
        nets=[
            NetIntent(name="VDD33", kind="power", pins=[
                LogicalPin(ref="U1", pin="VDD"),
                LogicalPin(ref="C10", pin="1"),
                LogicalPin(ref="C10", pin="2"),
                LogicalPin(ref="C1", pin="1"),
            ]),
            NetIntent(name="GND", kind="ground", pins=[
                LogicalPin(ref="U1", pin="VSS"),
                LogicalPin(ref="C1", pin="2"),
            ]),
        ],
        ground_net="GND",
        supply_nets=["VDD33"],
    )


def _nets_of(plan: NetlistIntent) -> dict[str, list[str]]:
    return {n.name: [f"{p.ref}:{p.pin}" for p in n.pins] for n in plan.nets}


def test_bridged_capacitor_gets_its_return_path() -> None:
    """The C10 defect: both pads on VDD33 because no fourth supply pin was left."""
    fixes: list[str] = []
    out = _normalize_bridged_capacitor_return(_selection(), _bridged_plan(), fixes)
    nets = _nets_of(out)
    assert "C10:1" in nets["VDD33"]
    assert "C10:2" in nets["GND"]
    assert nets["VDD33"].count("C10:1") == 1
    assert len(fixes) == 1
    # The trace has to name the part, both nets, and what was done.
    assert "C10" in fixes[0]
    assert "VDD33" in fixes[0]
    assert "GND" in fixes[0]


def test_correction_is_idempotent() -> None:
    fixes: list[str] = []
    once = _normalize_bridged_capacitor_return(_selection(), _bridged_plan(), fixes)
    again_fixes: list[str] = []
    twice = _normalize_bridged_capacitor_return(_selection(), once, again_fixes)
    assert again_fixes == []
    assert _nets_of(twice) == _nets_of(once)


def test_capacitor_bridged_across_ground_is_left_alone() -> None:
    """Moving a terminal to ground changes nothing; which rail it belongs on is
    a design decision, not a correction."""
    plan = NetlistIntent(
        nets=[NetIntent(name="GND", kind="ground", pins=[
            LogicalPin(ref="C10", pin="1"),
            LogicalPin(ref="C10", pin="2"),
        ])],
        ground_net="GND",
    )
    fixes: list[str] = []
    out = _normalize_bridged_capacitor_return(_selection(), plan, fixes)
    assert fixes == []
    assert _nets_of(out) == _nets_of(plan)


def test_multi_pin_device_on_one_net_is_left_alone() -> None:
    """Two supply pins of an MCU on one rail is normal, not a bridge."""
    plan = NetlistIntent(
        nets=[NetIntent(name="VDD33", kind="power", pins=[
            LogicalPin(ref="U1", pin="VDD"),
            LogicalPin(ref="U1", pin="VDDA"),
        ])],
        ground_net="GND",
    )
    fixes: list[str] = []
    assert _nets_of(_normalize_bridged_capacitor_return(_selection(), plan, fixes)) == (
        _nets_of(plan)
    )
    assert fixes == []


def test_correctly_wired_capacitor_is_left_alone() -> None:
    plan = NetlistIntent(
        nets=[
            NetIntent(name="VDD33", kind="power", pins=[LogicalPin(ref="C1", pin="1")]),
            NetIntent(name="GND", kind="ground", pins=[LogicalPin(ref="C1", pin="2")]),
        ],
        ground_net="GND",
    )
    fixes: list[str] = []
    _normalize_bridged_capacitor_return(_selection(), plan, fixes)
    assert fixes == []


def test_unknown_reference_is_not_corrected() -> None:
    """A part absent from the selection is not this step's to rewire."""
    plan = NetlistIntent(
        nets=[NetIntent(name="VDD33", kind="power", pins=[
            LogicalPin(ref="C99", pin="1"),
            LogicalPin(ref="C99", pin="2"),
        ])],
        ground_net="GND",
    )
    fixes: list[str] = []
    _normalize_bridged_capacitor_return(_selection(), plan, fixes)
    assert fixes == []


def test_duplicate_pin_declaration_is_left_to_its_own_check() -> None:
    """The same pin listed twice is ``no_double_assigned_pins``, not a bridge.

    Relocating one of them would remove the evidence of the duplicate.
    """
    plan = NetlistIntent(
        nets=[NetIntent(name="VDD33", kind="power", pins=[
            LogicalPin(ref="C10", pin="1"),
            LogicalPin(ref="C10", pin="1"),
        ])],
        ground_net="GND",
    )
    fixes: list[str] = []
    _normalize_bridged_capacitor_return(_selection(), plan, fixes)
    assert fixes == []


def test_non_numeric_pin_identifiers_are_preserved() -> None:
    """Nothing assumes a capacitor's terminals are numbered "1" and "2"."""
    plan = NetlistIntent(
        nets=[NetIntent(name="VDD33", kind="power", pins=[
            LogicalPin(ref="C10", pin="A"),
            LogicalPin(ref="C10", pin="K"),
        ])],
        ground_net="GND",
    )
    fixes: list[str] = []
    out = _normalize_bridged_capacitor_return(_selection(), plan, fixes)
    assert _nets_of(out)["GND"] == ["C10:K"]
    assert "pin K" in fixes[0]


def test_correction_does_not_depend_on_the_symbol_library() -> None:
    """An empty pin list must not switch the correction off.

    ``symbols.symbol_pins`` reports "library unreachable" and "symbol unknown"
    the same way, so a pass that gated on it would go quiet on a machine with a
    stale ``KICAD_SYMBOL_DIR`` — and it would look exactly like "no capacitor was
    bridged".
    """
    from ratsnestpro.eda import symbols as symbols_module

    original = symbols_module.symbol_pins
    try:
        symbols_module.symbol_pins = lambda _lib_id: []  # type: ignore[assignment]
        fixes: list[str] = []
        out = _normalize_bridged_capacitor_return(
            _selection(), _bridged_plan(), fixes
        )
    finally:
        symbols_module.symbol_pins = original  # type: ignore[assignment]
    assert len(fixes) == 1
    assert "C10:2" in _nets_of(out)["GND"]


# --------------------------------------------------------------------------- #
# The second deterministic correction: repeated supply pins share one net
# --------------------------------------------------------------------------- #

# An LQFP-48 STM32's pins, trimmed to what the pass reads. ``VDD`` appears three
# times because one die rail is bonded out three times; ``VDDA`` is a separate
# name because it is a separate rail.
_STM32_PINS = [
    {"number": "1", "name": "VBAT", "type": "power_in"},
    {"number": "2", "name": "PC13", "type": "bidirectional"},
    {"number": "9", "name": "VDDA", "type": "power_in"},
    {"number": "23", "name": "VSS", "type": "power_in"},
    {"number": "24", "name": "VDD", "type": "power_in"},
    {"number": "35", "name": "VSS", "type": "power_in"},
    {"number": "36", "name": "VDD", "type": "power_in"},
    {"number": "48", "name": "VDD", "type": "power_in"},
]


@pytest.fixture
def stm32_pins(monkeypatch):
    from ratsnestpro.eda import symbols as symbols_module

    monkeypatch.setattr(
        symbols_module,
        "symbol_pins",
        lambda lib_id: _STM32_PINS if lib_id.startswith("MCU_ST") else [],
    )


def _partial_supply_plan() -> NetlistIntent:
    """``VDD`` wired on pin 24 only; 36 and 48 left floating.

    Both ``VSS`` pins are wired, so that name group is already consistent and any
    correction reported below is about ``VDD`` alone.
    """
    return NetlistIntent(
        nets=[
            NetIntent(name="VDD33", kind="power", pins=[
                LogicalPin(ref="U1", pin="24"),
                LogicalPin(ref="C1", pin="1"),
            ]),
            NetIntent(name="GND", kind="ground", pins=[
                LogicalPin(ref="U1", pin="23"),
                LogicalPin(ref="U1", pin="35"),
                LogicalPin(ref="C1", pin="2"),
            ]),
        ],
        ground_net="GND",
        supply_nets=["VDD33"],
    )


def test_unwired_siblings_of_a_wired_supply_pin_are_attached(stm32_pins) -> None:
    """The die cannot hold VDD at two potentials, so the answer is not a choice."""
    fixes: list[str] = []
    out = _normalize_duplicate_supply_pins(
        _selection(), _partial_supply_plan(), fixes
    )
    nets = _nets_of(out)
    assert "U1:36" in nets["VDD33"]
    assert "U1:48" in nets["VDD33"]
    assert len(fixes) == 1, f"VDD only; VSS was already consistent: {fixes}"
    assert "VDD" in fixes[0]
    assert "VDD33" in fixes[0]
    # A separate name is a separate rail, whatever it is next to in the package.
    assert "U1:9" not in nets["VDD33"]


def test_disagreeing_supply_pins_are_left_for_the_checks(stm32_pins) -> None:
    """One name on two nets is a claim this pass has no basis to adjudicate."""
    plan = NetlistIntent(
        nets=[
            NetIntent(name="VDD33", kind="power", pins=[
                LogicalPin(ref="U1", pin="24"),
            ]),
            NetIntent(name="VDD5", kind="power", pins=[
                LogicalPin(ref="U1", pin="36"),
            ]),
            NetIntent(name="GND", kind="ground", pins=[
                LogicalPin(ref="U1", pin="23"),
                LogicalPin(ref="U1", pin="35"),
            ]),
        ],
        ground_net="GND",
        supply_nets=["VDD33", "VDD5"],
    )
    fixes: list[str] = []
    out = _normalize_duplicate_supply_pins(_selection(), plan, fixes)
    assert fixes == []
    assert _nets_of(out) == _nets_of(plan)


def test_a_name_with_nothing_wired_is_left_alone(stm32_pins) -> None:
    """No wired sibling means there is no answer to read off."""
    plan = NetlistIntent(
        nets=[NetIntent(name="GND", kind="ground", pins=[
            LogicalPin(ref="U1", pin="23"),
            LogicalPin(ref="U1", pin="35"),
        ])],
        ground_net="GND",
    )
    fixes: list[str] = []
    out = _normalize_duplicate_supply_pins(_selection(), plan, fixes)
    assert fixes == []
    assert _nets_of(out) == _nets_of(plan)


def test_a_stated_no_connect_is_not_overwritten(stm32_pins) -> None:
    """A supply pin marked unused is a defect, but a stated one.

    Wiring it would remove the evidence instead of the fault.
    """
    plan = _partial_supply_plan().model_copy(
        update={"no_connect_pins": [LogicalPin(ref="U1", pin="48")]},
        deep=True,
    )
    fixes: list[str] = []
    out = _normalize_duplicate_supply_pins(_selection(), plan, fixes)
    nets = _nets_of(out)
    assert "U1:36" in nets["VDD33"]
    assert "U1:48" not in nets["VDD33"]


def test_supply_pin_correction_is_idempotent(stm32_pins) -> None:
    fixes: list[str] = []
    once = _normalize_duplicate_supply_pins(
        _selection(), _partial_supply_plan(), fixes
    )
    again: list[str] = []
    twice = _normalize_duplicate_supply_pins(_selection(), once, again)
    assert again == []
    assert _nets_of(twice) == _nets_of(once)


def test_no_symbol_library_means_no_supply_pin_correction(monkeypatch) -> None:
    """Fail open. An unreachable library must not read as "nothing is repeated"."""
    from ratsnestpro.eda import symbols as symbols_module

    monkeypatch.setattr(symbols_module, "symbol_pins", lambda _lib_id: None)
    fixes: list[str] = []
    out = _normalize_duplicate_supply_pins(
        _selection(), _partial_supply_plan(), fixes
    )
    assert fixes == []
    assert _nets_of(out) == _nets_of(_partial_supply_plan())


# --------------------------------------------------------------------------- #
# In-step repair feedback
# --------------------------------------------------------------------------- #


def test_failure_line_carries_class_and_targets() -> None:
    line = _repair_failure_line(CheckResult(
        name="supply_pin_not_on_regulator_input:U1",
        ok=False,
        message="U1:1 (VBAT) sits on /REG_IN",
        targets=["U1", "U2", "/REG_IN"],
    ))
    assert "[erc_violation]" in line
    assert "targets: U1, U2, /REG_IN" in line
    assert "U1:1 (VBAT) sits on /REG_IN" in line


def test_failure_line_claims_no_structure_it_does_not_have() -> None:
    line = _repair_failure_line(CheckResult(name="no_such_check", ok=False, message="m"))
    assert line == "- no_such_check: m"


def test_directives_are_deduplicated() -> None:
    """Twelve failures of one class are still one instruction."""
    names = [f"two_terminal_not_shorted:C{i}" for i in range(12)]
    assert len(repair_directives(names)) == 1


def test_directives_keep_first_seen_order() -> None:
    directives = repair_directives([
        "no_double_assigned_pins",              # pin_conflict
        "two_terminal_not_shorted:C10",         # erc_violation
        "datasheet_limits:U1:supply_range",     # constraint_violation
    ])
    assert [d.split(":", 1)[0] for d in directives] == [
        "pin_conflict",
        "erc_violation",
        "constraint_violation",
    ]


def test_unmapped_check_contributes_no_directive() -> None:
    """``unknown`` is reserved for failures classified as unknown, not for
    checks that simply have no entry."""
    assert repair_directives(["no_such_check"]) == []


def test_every_declared_class_has_a_directive() -> None:
    """A new failure class must not reach the model with no instruction.

    This is the guard that makes the two tables stay in step: adding a class to
    ``CHECK_FAILURE_CLASS`` without a directive fails here rather than silently
    producing feedback that says only "this was rejected".
    """
    missing = sorted(set(CHECK_FAILURE_CLASS.values()) - set(CLASS_REPAIR_DIRECTIVE))
    assert not missing, f"failure classes with no repair directive: {missing}"


def test_do_not_repair_classes_say_so() -> None:
    """For a class whose fix is not an edit, the instruction must not ask for one.

    ``constraint_violation`` is a datasheet limit and ``tool_unavailable`` is a
    missing binary; telling the model to rewire either would spend a round
    producing something that still fails.
    """
    for cls in ("constraint_violation", "tool_unavailable", "harness_defect"):
        text = CLASS_REPAIR_DIRECTIVE[cls].lower()
        assert "do not" in text or "unchanged" in text, cls


# --------------------------------------------------------------------------- #
# The diagnosis side, when it is importable
# --------------------------------------------------------------------------- #


def _diagnosis_module():
    import importlib.util
    from pathlib import Path

    import ratsnestpro

    # tests -> RatsNestPro-main -> RatsNestPro-main -> src -> repo src/agents
    root = Path(ratsnestpro.__file__).resolve().parents[4]
    path = root / "agents" / "ratsnestpro" / "diagnosis.py"
    if not path.is_file():
        pytest.skip("diagnosis module not on disk")
    spec = importlib.util.spec_from_file_location("rnp_diagnosis_under_test", path)
    if spec is None or spec.loader is None:
        pytest.skip("diagnosis module not loadable")
    import sys

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_declared_class_and_targets_win_over_inference() -> None:
    """Loaded by path: the ``agents`` package needs langgraph, which this
    environment does not install."""
    m = _diagnosis_module()
    d = m.classify_failure(
        "U1:1 (VBAT) sits on /REG_IN",
        step="sch_connections",
        check_name="supply_pin_not_on_regulator_input:U1",
        declared_class="erc_violation",
        targets=["U1", "U2", "/REG_IN", "/VDD33"],
    )
    assert d.failure_class == "erc_violation"
    assert d.targets == ("U1", "U2", "/REG_IN", "/VDD33")


def test_an_unrecognised_declared_class_is_discarded() -> None:
    """A newer engine could name a class this diagnoser has no strategy for."""
    m = _diagnosis_module()
    d = m.classify_failure(
        "x", check_name="two_terminal_not_shorted:C10", declared_class="bogus"
    )
    assert d.failure_class == "erc_violation"


def test_payload_without_the_new_fields_still_works() -> None:
    m = _diagnosis_module()
    report = m.FailureDiagnoser().diagnose_pipeline_result({
        "steps": [{
            "name": "sch_connections",
            "blocked": True,
            "summary": "",
            "failed_checks": [{
                "name": "two_terminal_not_shorted:C10",
                "message": "C10 has both terminals on VDD33",
            }],
        }]
    })
    assert [d.failure_class for d in report.diagnoses] == ["erc_violation"]
    # Falls back to the prose regex, which finds the reference but not the net.
    assert report.diagnoses[0].targets == ("C10",)
