"""Task 12 — the gate that asks the user before building a damaging board.

The policy under test is small and the whole point is that it is visible:

* a cited datasheet limit broken without acknowledgement stops the design;
* an acknowledged one becomes a WARNING that is never removed, so an accepted
  risk stays distinguishable from a risk nobody found;
* an opinion without a page reference never blocks at all.
"""

from __future__ import annotations

from ratsnestpro.agents.llm import LlmMode
from ratsnestpro.domain.contracts import Severity
from ratsnestpro.eda import factclaim
from ratsnestpro.orchestration.pipeline import (
    PipelineContext,
    PipelineState,
    PipelineStep,
    RequirementsStep,
)

_DANGEROUS = "Build an STM32F103C8T6 board and power the MCU from 5 V."
_SAFE = "Build an STM32F103C8T6 board and power the MCU from 3.3 V."
_ACKED = f"{_DANGEROUS}\nACK-RISK: supply_range=5"


def _run(text: str) -> tuple[PipelineState, list]:
    state = PipelineState(requirement_text=text)
    result = RequirementsStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    return state, result.checks


def _conflicts(checks: list) -> list:
    return [c for c in checks if c.name.startswith("user_claim_conflict")]


# --------------------------------------------------------------------------- #
# What counts as a claim at all
# --------------------------------------------------------------------------- #


def test_appended_evidence_is_not_read_as_a_user_claim() -> None:
    """A number in the evidence is a fact, not a request.

    ``requirement_text`` grows during a run: the architect appends grounded
    evidence, and datasheet prose is dense with figures. A shipped run stopped at
    step 1 demanding acknowledgement of "clock_external = 36 MHz", quoted out of
    a sentence describing the chip's own APB bus, so the user was asked to accept
    a risk that appeared nowhere in what they had written.
    """
    evidence = (
        "GROUNDED ARCHITECT EVIDENCE\n"
        "STM32F103x8/xB DS5319: the low-speed APB domain is 36 MHz. See "
        "Figure 2 for details. Absolute maximum VDD is 4 V."
    )
    state, checks = _run(f"{_SAFE}\n{evidence}")
    slots = {v.slot for v in state.claim_verdicts}
    assert "clock_external" not in slots, slots
    assert _conflicts(checks) == []


def test_a_value_the_user_did_state_still_arbitrates_with_evidence_present() -> None:
    """Stripping the evidence must not strip the requirement with it."""
    state, checks = _run(
        f"{_DANGEROUS}\nGROUNDED ARCHITECT EVIDENCE\nVDD standard range 2 to 3.6 V."
    )
    names = [c.name for c in _conflicts(checks)]
    assert names == ["user_claim_conflict:supply_range=5"], names


# --------------------------------------------------------------------------- #
# Blocking
# --------------------------------------------------------------------------- #


def test_an_unacknowledged_hard_conflict_blocks_the_step() -> None:
    state = PipelineState(requirement_text=_DANGEROUS)
    result = RequirementsStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    assert result.blocked, "a broken datasheet limit must stop the design"
    conflict = _conflicts(result.checks)
    assert len(conflict) == 1
    assert conflict[0].name == "user_claim_conflict:supply_range=5"
    assert not conflict[0].ok
    assert conflict[0].severity is Severity.ERROR


def test_the_blocking_message_carries_the_page_and_the_exact_token() -> None:
    _, checks = _run(_DANGEROUS)
    message = _conflicts(checks)[0].message
    assert "DS5319" in message, message
    assert "Table 9" in message or "p.38" in message, message
    assert "ACK-RISK: supply_range=5" in message, message
    assert "DAMAGE" in message


def test_a_legal_value_produces_no_conflict_and_does_not_block() -> None:
    state = PipelineState(requirement_text=_SAFE)
    result = RequirementsStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    assert not result.blocked
    assert _conflicts(result.checks) == []


def test_a_requirement_with_no_stated_values_is_unaffected() -> None:
    state = PipelineState(requirement_text="Build a plain STM32F103C8T6 breakout.")
    result = RequirementsStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    assert not result.blocked
    assert _conflicts(result.checks) == []
    assert state.claim_verdicts == []


# --------------------------------------------------------------------------- #
# Acknowledgement: downgraded, never deleted
# --------------------------------------------------------------------------- #


def test_an_acknowledged_conflict_unblocks_but_stays_on_the_record() -> None:
    state = PipelineState(requirement_text=_ACKED)
    result = RequirementsStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    assert not result.blocked, "the user chose to proceed"
    conflict = _conflicts(result.checks)
    assert len(conflict) == 1, "the finding must NOT be deleted"
    assert conflict[0].ok, "it no longer fails"
    assert conflict[0].severity is Severity.WARNING
    assert "ACCEPTED RISK" in conflict[0].message
    assert "DS5319" in conflict[0].message, "an accepted risk keeps its citation"


def test_accepted_risks_are_readable_from_the_state() -> None:
    state = PipelineState(requirement_text=_ACKED)
    RequirementsStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    accepted = state.accepted_risks()
    assert len(accepted) == 1
    assert accepted[0].slot == "supply_range"
    assert accepted[0].claim.value == 5.0
    assert accepted[0].citation


def test_an_ack_is_scoped_and_does_not_waive_a_second_conflict() -> None:
    """Two conflicts, one ack: the other must still block.

    Also pins that the two checks get DISTINCT names. Both land on a voltage
    slot, and if they shared a name one of them would be silently dropped by
    anything that keys findings by name.
    """
    text = (
        "Feed the AMS1117-3.3 from 24 V and power the STM32F103C8T6 from 5 V.\n"
        "ACK-RISK: supply_range=5"
    )
    state = PipelineState(requirement_text=text)
    result = RequirementsStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    conflicts = _conflicts(result.checks)
    names = [c.name for c in conflicts]
    assert len(names) == len(set(names)), names

    by_name = {c.name: c for c in conflicts}
    assert by_name["user_claim_conflict:supply_range=5"].ok, "acknowledged"
    assert not by_name["user_claim_conflict:abs_max_vin=24"].ok, "not acknowledged"
    assert result.blocked


def test_a_clause_naming_a_regulator_routes_to_its_input_limits() -> None:
    """"Feed the AMS1117 from 24 V" is about the REGULATOR, not the MCU.

    Distance alone gets this wrong: in the sentence below the STM32 is closer to
    "24 V" than the AMS1117 is, so a nearest-device rule would check the MCU's
    supply range and never look at the regulator's 15 V absolute maximum.
    """
    text = "Feed the AMS1117-3.3 from 24 V and power the STM32F103C8T6 from 5 V."
    state = PipelineState(requirement_text=text)
    RequirementsStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    by_slot = {v.slot: v.claim.value for v in state.claim_verdicts}
    assert by_slot["abs_max_vin"] == 24.0
    assert by_slot["supply_range"] == 5.0


def test_changing_the_value_invalidates_the_acknowledgement() -> None:
    text = "power the STM32F103C8T6 from 4.5 V\nACK-RISK: supply_range=5"
    state = PipelineState(requirement_text=text)
    result = RequirementsStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    assert result.blocked, "an ack for 5 V must not waive a 4.5 V conflict"


# --------------------------------------------------------------------------- #
# The advisory tier never blocks
# --------------------------------------------------------------------------- #


def test_offline_mode_runs_no_experience_check_and_never_blocks_on_one() -> None:
    """Offline is supported; a missing soft opinion must not stop anything."""
    text = "Fit a 10 uF input capacitor on the AMS1117-3.3."
    state = PipelineState(requirement_text=text)
    result = RequirementsStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    assert not result.blocked
    assert all(v.tier != "advisory" for v in state.claim_verdicts)
    assert all(v.ok for v in state.claim_verdicts)


def test_an_out_of_practice_advisory_warns_but_does_not_block(monkeypatch) -> None:
    text = "Fit a 10 uF input capacitor on the AMS1117-3.3."

    def fake_asker(self, ctx, knowledge):  # noqa: ANN001, ANN202
        return lambda verdict: factclaim.ExperienceOpinion(
            within_norm=False, typical_range="1-22 uF", reason="unusual here"
        )

    monkeypatch.setattr(RequirementsStep, "_experience_asker", fake_asker)
    state = PipelineState(requirement_text=text)
    result = RequirementsStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))

    conflict = _conflicts(result.checks)
    assert conflict, "an out-of-practice value must be surfaced"
    assert not result.blocked, "experience has no standing to block"
    assert all(c.severity is not Severity.ERROR for c in conflict)
    assert "EXPERIENCE, not a manual figure" in conflict[0].message


def test_check_is_deterministic_and_reads_only_stored_verdicts() -> None:
    """``check`` must not consult a model: same state, same checks, twice."""
    state = PipelineState(requirement_text=_DANGEROUS)
    step = RequirementsStep()
    step.run(state, PipelineContext(mode=LlmMode.OFFLINE))
    artifact = state.artifact(PipelineStep.REQUIREMENTS)
    assert artifact is not None
    first = [(c.name, c.ok, c.message) for c in step.check(state, artifact)]
    second = [(c.name, c.ok, c.message) for c in step.check(state, artifact)]
    assert first == second

    state.claim_verdicts = []
    assert _conflicts(step.check(state, artifact)) == []
