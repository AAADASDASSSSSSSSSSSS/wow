"""Tasks 7-8 — datasheet facts reaching the steps that propose designs.

Before this wiring the pipeline only ever met a datasheet limit by violating it:
``SelectionStep.check`` rejected a proposal with ``datasheet_limits:*`` and the
repair loop guessed again. These tests pin the other direction — the facts are in
the prompt before the proposal exists — and pin the two properties that make the
wiring safe: hard facts stay in their own block, and no step reads another's.
"""

from __future__ import annotations

from ratsnestpro.eda import factbrief
from ratsnestpro.orchestration.pipeline import (
    _FACT_AUTHORITY,
    ALL_STEPS,
    PipelineContext,
    PipelineState,
    PipelineStep,
    SchConnectionsStep,
    SelectionStep,
    TopologyStep,
    _facts_block,
)
from ratsnestpro.orchestration.pipeline_contracts import SelectedPart, SelectionPlan

_STM32_REQUIREMENT = (
    "Two-layer STM32F103C8T6 board powered from USB-C through an AMS1117-3.3, "
    "with an 8 MHz crystal and SWD header."
)


def _selection() -> SelectionPlan:
    return SelectionPlan(
        parts=[
            SelectedPart(
                ref="U1", symbol="MCU_ST_STM32F1:STM32F103C8Tx",
                value="STM32F103C8T6", footprint="", role="mcu",
            ),
            SelectedPart(
                ref="U2", symbol="Regulator_Linear:AMS1117-3.3",
                value="AMS1117-3.3", footprint="", role="ldo_regulator",
            ),
            SelectedPart(
                ref="U3", symbol="MCU_ST_STM32F4:STM32F407VGTx",
                value="STM32F407VGT6", footprint="", role="mcu",
            ),
        ],
        rationale="test",
    )


# --------------------------------------------------------------------------- #
# Task 7 — the context field and its lifecycle
# --------------------------------------------------------------------------- #


def test_briefed_steps_are_exactly_the_steps_that_override_the_hook() -> None:
    """``factbrief.BRIEFED_STEPS`` must not drift from the pipeline.

    The contract test in ``test_factbrief_contract`` excuses three slots on the
    grounds that their only consumer is an unwired step. That excuse is only true
    while this set is accurate, so the two are checked against each other rather
    than each being trusted separately.
    """
    overriding = {
        type(step).__name__
        for step in ALL_STEPS
        if "fact_sheets_for_step" in type(step).__dict__
    }
    assert overriding == set(factbrief.BRIEFED_STEPS), (
        f"pipeline overrides {sorted(overriding)} but BRIEFED_STEPS says "
        f"{sorted(factbrief.BRIEFED_STEPS)}"
    )


def test_fact_brief_is_installed_before_propose_and_cleared_after() -> None:
    """Same lifecycle as ``repair_feedback``: present during, absent between."""
    seen: list[str] = []
    state = PipelineState(requirement_text=_STM32_REQUIREMENT)
    ctx = PipelineContext()

    step = TopologyStep()
    original = step.propose

    def spy(state_, ctx_, knowledge):  # noqa: ANN001, ANN202
        seen.append(ctx_.fact_brief)
        return original(state_, ctx_, knowledge)

    step.propose = spy  # type: ignore[method-assign]
    result = step.run(state, ctx)

    assert seen and seen[0], "propose must see a populated brief"
    assert "STM32F103" in seen[0]
    assert ctx.fact_brief == "", "the brief must not leak past the step"
    assert result.facts_used, "the facts shown must be recorded"


def test_facts_used_names_device_and_slot() -> None:
    state = PipelineState(requirement_text=_STM32_REQUIREMENT)
    result = TopologyStep().run(state, PipelineContext())
    assert all(":" in entry for entry in result.facts_used)
    assert any(entry.startswith("STM32F103:") for entry in result.facts_used)
    assert any(entry.endswith(":supply_rails") for entry in result.facts_used)


def test_unwired_step_gets_no_brief_and_records_no_facts() -> None:
    from ratsnestpro.orchestration.pipeline import LayoutPartitionStep

    state = PipelineState(requirement_text=_STM32_REQUIREMENT)
    ctx = PipelineContext()
    state.artifacts[PipelineStep.SELECTION] = _selection()
    result = LayoutPartitionStep().run(state, ctx)
    assert result.facts_used == []
    assert ctx.fact_brief == ""


def test_a_requirement_naming_nothing_known_produces_no_brief() -> None:
    state = PipelineState(requirement_text="a generic two-layer breakout board")
    result = TopologyStep().run(state, PipelineContext())
    assert result.facts_used == []


# --------------------------------------------------------------------------- #
# Task 8 — the prompt block
# --------------------------------------------------------------------------- #


def test_facts_block_is_labelled_and_empty_when_there_is_nothing() -> None:
    ctx = PipelineContext()
    assert _facts_block(ctx) == ""
    ctx.fact_brief = "STM32F103 (U1) — DS5319\n  - supply_range: 2-3.6 V  [Table 9 p.38]"
    block = _facts_block(ctx)
    assert block.startswith("Datasheet facts (authoritative, cited):")
    assert "supply_range" in block


def test_hard_facts_are_a_separate_block_from_soft_knowledge() -> None:
    """``knowledge.store`` says corpus text is "never treated as fact".

    Merging the two would erase that, so the prompt must carry two labelled
    sections and the fact section must come first.
    """
    captured: dict[str, str] = {}
    state = PipelineState(requirement_text=_STM32_REQUIREMENT)
    ctx = PipelineContext()

    import ratsnestpro.orchestration.pipeline as pipeline

    original = pipeline.propose_structured

    def spy(ctx_, *, model, system, user, fallback):  # noqa: ANN001, ANN202
        captured["system"] = system
        captured["user"] = user
        return original(ctx_, model=model, system=system, user=user, fallback=fallback)

    pipeline.propose_structured = spy  # type: ignore[assignment]
    try:
        TopologyStep().run(state, ctx)
    finally:
        pipeline.propose_structured = original  # type: ignore[assignment]

    user = captured["user"]
    assert "Datasheet facts (authoritative, cited):" in user
    assert "Knowledge:" in user
    assert user.index("Datasheet facts") < user.index("Knowledge:"), (
        "facts must not be buried after advisory prose"
    )
    assert _FACT_AUTHORITY in captured["system"]


def test_authority_wording_forbids_inventing_a_missing_value() -> None:
    """The instruction that stops silence being read as freedom."""
    assert "NOT STATED" in _FACT_AUTHORITY
    assert "never as" in _FACT_AUTHORITY and "unlimited" in _FACT_AUTHORITY
    assert "do not substitute a number" in _FACT_AUTHORITY.lower()
    assert "missing evidence, not free of constraints" in _FACT_AUTHORITY


def test_topology_sees_supply_rails_and_selection_sees_supply_range() -> None:
    """Each step is shown what its own decisions need, per the consumer registry."""
    state = PipelineState(requirement_text=_STM32_REQUIREMENT)
    ctx = PipelineContext()

    topology = TopologyStep()
    topology._install_fact_brief(state, ctx)
    topology_brief = ctx.fact_brief
    assert "supply_rails" in topology_brief
    assert "supply_range" not in topology_brief

    selection = SelectionStep()
    selection._install_fact_brief(state, ctx)
    assert "supply_range" in ctx.fact_brief


def test_connections_step_briefs_the_real_selection_and_flags_uncovered_parts() -> None:
    state = PipelineState(requirement_text=_STM32_REQUIREMENT)
    state.artifacts[PipelineStep.SELECTION] = _selection()
    ctx = PipelineContext()

    step = SchConnectionsStep()
    facts_used = step._install_fact_brief(state, ctx)
    brief = ctx.fact_brief

    assert "STM32F103 (U1)" in brief, brief
    assert "AMS1117-3.3 (U2)" in brief, brief
    assert "decoupling" in brief
    assert "STM32F407VGT6" in brief, "an uncovered part must be named"
    assert "missing evidence, not permission" in brief
    assert any(entry.startswith("STM32F103:") for entry in facts_used)


def test_internal_ldo_warning_reaches_the_topology_prompt() -> None:
    """The RP2040's 1.1 V core is an LDO OUTPUT; feeding it damages the part."""
    state = PipelineState(requirement_text="RP2040 board with 12 MHz crystal")
    ctx = PipelineContext()
    TopologyStep()._install_fact_brief(state, ctx)
    assert "MUST NOT be driven by the board" in ctx.fact_brief, ctx.fact_brief


def test_offline_fallback_still_produces_an_artifact_with_facts_present() -> None:
    """Injection must not disturb the deterministic path."""
    state = PipelineState(requirement_text=_STM32_REQUIREMENT)
    result = TopologyStep().run(state, PipelineContext())
    plan = state.artifact(PipelineStep.TOPOLOGY)
    assert plan is not None and plan.rails
    assert not result.blocked
