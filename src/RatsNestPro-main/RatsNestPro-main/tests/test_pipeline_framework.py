"""Task 4: fixed pipeline framework — order enforcement, fail-closed, fake LLM."""

from __future__ import annotations

import json

import pytest

from ratsnestpro.agents.llm import LlmError, LlmMode
from ratsnestpro.orchestration.pipeline import (
    ALL_STEPS,
    CANONICAL_ORDER,
    Pipeline,
    PipelineContext,
    PipelineOrderError,
    PipelineState,
    PipelineStep,
    RequirementsStep,
    TopologyStep,
)
from ratsnestpro.orchestration.pipeline_contracts import TopologyPlan


class FakeLLM:
    """Returns canned JSON per requested model, recording calls."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        self.calls += 1
        return self._responses.pop(0) if self._responses else "{}"


def test_offline_skeleton_runs_two_steps() -> None:
    state = PipelineState(requirement_text="ATmega328 USB-C 3.3V dev board")
    Pipeline([RequirementsStep(), TopologyStep()]).run(
        state, PipelineContext(mode=LlmMode.OFFLINE), until=PipelineStep.TOPOLOGY
    )
    assert state.completed == [PipelineStep.REQUIREMENTS, PipelineStep.TOPOLOGY]
    assert not state.blocked
    topo = state.artifact(PipelineStep.TOPOLOGY)
    assert isinstance(topo, TopologyPlan)
    assert topo.rails and topo.blocks
    # Offline: no LLM used.
    assert all(not r.used_llm for r in state.results)


def test_five_volt_rail_request_does_not_collide_with_the_input_rail() -> None:
    """A 5 V logic rail must not be declared twice.

    ``TopologyPlan`` rejects duplicate rails, and the deterministic fallback runs
    where construction errors are not caught — so the duplicate surfaced as an
    uncaught ValidationError instead of a gate verdict. ATmega328 at 16 MHz
    requires >= 4.5 V, so this is the ordinary path for that speed grade, not an
    edge case.
    """
    state = PipelineState(requirement_text="ATmega328 USB-C 5V 16MHz dev board")
    Pipeline([RequirementsStep(), TopologyStep()]).run(
        state, PipelineContext(mode=LlmMode.OFFLINE), until=PipelineStep.TOPOLOGY
    )
    assert not state.blocked
    topo = state.artifact(PipelineStep.TOPOLOGY)
    assert isinstance(topo, TopologyPlan)
    assert topo.rails == ["5V"]


def test_progress_callback_runs_after_each_completed_step() -> None:
    snapshots: list[tuple[PipelineStep, list[PipelineStep]]] = []
    state = PipelineState(requirement_text="ATmega328 USB-C 3.3V dev board")
    context = PipelineContext(
        mode=LlmMode.OFFLINE,
        on_step_completed=lambda current, result: snapshots.append(
            (result.step, current.completed.copy())
        ),
    )

    Pipeline([RequirementsStep(), TopologyStep()]).run(state, context)

    assert snapshots == [
        (PipelineStep.REQUIREMENTS, [PipelineStep.REQUIREMENTS]),
        (
            PipelineStep.TOPOLOGY,
            [PipelineStep.REQUIREMENTS, PipelineStep.TOPOLOGY],
        ),
    ]


def test_pipeline_resumes_after_completed_prefix_without_rerunning_it() -> None:
    state = PipelineState(requirement_text="ATmega328 USB-C 3.3V dev board")
    Pipeline([RequirementsStep()]).run(
        state, PipelineContext(mode=LlmMode.OFFLINE)
    )

    Pipeline([RequirementsStep(), TopologyStep()]).run(
        state, PipelineContext(mode=LlmMode.OFFLINE)
    )

    assert state.completed == [PipelineStep.REQUIREMENTS, PipelineStep.TOPOLOGY]
    assert len(state.results) == 2


def test_fake_llm_path_used_and_parsed() -> None:
    req = json.dumps({"raw_text": "board", "project_name": "demo"})
    topo = json.dumps(
        {
            "blocks": [{"name": "mcu", "kind": "mcu", "description": "the MCU"}],
            "rails": ["3.3V"],
            "ground_net": "GND",
            "rationale": "llm",
        }
    )
    fake = FakeLLM([req, topo])
    state = PipelineState(requirement_text="make a board")
    Pipeline([RequirementsStep(), TopologyStep()]).run(
        state, PipelineContext(mode=LlmMode.AUTO, client=fake)
    )
    assert fake.calls == 2
    assert all(r.used_llm for r in state.results)
    topo_art = state.artifact(PipelineStep.TOPOLOGY)
    assert isinstance(topo_art, TopologyPlan)
    assert topo_art.rationale == "llm"


def test_required_mode_fails_closed_without_client() -> None:
    state = PipelineState(requirement_text="board")
    with pytest.raises(LlmError):
        Pipeline([RequirementsStep()]).run(state, PipelineContext(mode=LlmMode.REQUIRED))


def test_required_mode_fails_closed_on_bad_json() -> None:
    fake = FakeLLM(["not json at all"])
    state = PipelineState(requirement_text="board")
    with pytest.raises(LlmError):
        Pipeline([RequirementsStep()]).run(
            state, PipelineContext(mode=LlmMode.REQUIRED, client=fake)
        )


def test_required_mode_can_checkpoint_the_failed_step() -> None:
    fake = FakeLLM(["not json at all"])
    state = PipelineState(requirement_text="board")

    Pipeline([RequirementsStep()]).run(
        state,
        PipelineContext(
            mode=LlmMode.REQUIRED,
            client=fake,
            capture_step_errors=True,
        ),
    )

    assert state.completed == [PipelineStep.REQUIREMENTS]
    assert state.blocked
    assert state.results[0].error_checks[0].name == "llm_proposal_failed"


def test_auto_mode_falls_back_on_bad_json() -> None:
    fake = FakeLLM(["garbage"])  # requirements step gets junk -> fallback
    state = PipelineState(requirement_text="fallback please")
    Pipeline([RequirementsStep()]).run(
        state, PipelineContext(mode=LlmMode.AUTO, client=fake)
    )
    assert not state.blocked
    assert state.results[0].used_llm is False  # fell back deterministically


def test_step_order_is_enforced() -> None:
    # Registering steps out of canonical order is rejected.
    with pytest.raises(PipelineOrderError):
        Pipeline([TopologyStep(), RequirementsStep()])
    # A non-prefix (starting mid-sequence) is rejected too.
    with pytest.raises(PipelineOrderError):
        Pipeline([TopologyStep()])


def test_default_pipeline_prefix_is_valid() -> None:
    # ALL_STEPS must be a valid canonical prefix (constructor validates).
    pipe = Pipeline()
    assert [s.step for s in pipe.steps] == CANONICAL_ORDER[: len(ALL_STEPS)]


def test_blocked_step_halts_pipeline() -> None:
    # A topology proposal with no rails fails the bottom-line check and blocks,
    # so the pipeline stops (fail closed) — here it's the last step anyway, but
    # the result must be marked blocked.
    bad_topo = json.dumps({"blocks": [], "rails": [], "ground_net": "GND"})
    fake = FakeLLM([json.dumps({"raw_text": "b", "project_name": "p"}), bad_topo])
    state = PipelineState(requirement_text="b")
    Pipeline([RequirementsStep(), TopologyStep()]).run(
        state, PipelineContext(mode=LlmMode.AUTO, client=fake)
    )
    assert state.blocked
    topo_result = state.results[-1]
    assert topo_result.step == PipelineStep.TOPOLOGY
    assert topo_result.error_checks
