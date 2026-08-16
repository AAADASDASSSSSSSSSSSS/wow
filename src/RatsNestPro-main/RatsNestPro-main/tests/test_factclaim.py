"""Task 9 — reading the numbers a user actually asked for.

A false positive here costs more than a miss: it interrupts the user to argue
about a value they never gave, which trains them to dismiss the warning. Most of
these tests are therefore about what must NOT be extracted.
"""

from __future__ import annotations

import pytest

from ratsnestpro.domain.contracts import Severity
from ratsnestpro.eda.factclaim import (
    EXPERIENCE_SYSTEM,
    Arbitration,
    ClaimVerdict,
    ExperienceOpinion,
    ack_token,
    arbitrate,
    experience_prompt,
    extract_claims,
    judge_by_experience,
    parse_acks,
)
from ratsnestpro.eda.factsheet import fact_sheet


def _slots(text: str) -> dict[str, float]:
    """slot -> value, flattened. Convenient for asserting on intent."""
    out: dict[str, float] = {}
    for claim in extract_claims(text):
        for slot in claim.slots:
            out[slot] = claim.value
    return out


# --------------------------------------------------------------------------- #
# The claims that matter
# --------------------------------------------------------------------------- #


def test_logic_supply_voltage_in_english() -> None:
    found = _slots("Power the STM32F103C8T6 from a 5 V rail.")
    assert found["supply_range"] == 5.0


def test_logic_supply_voltage_in_chinese() -> None:
    found = _slots("给 STM32F103C8T6 供电 5V")
    assert found["supply_range"] == 5.0


def test_regulator_input_voltage_maps_to_both_input_slots() -> None:
    """A stated input is a claim against the range AND the absolute maximum.

    The AMS1117 publishes only the absolute maximum, the AP2112 only the
    recommended range; a claim naming one slot would pass whichever part
    documents the other.
    """
    found = _slots("24 V input into the AMS1117-3.3")
    assert found["vin_range"] == 24.0
    assert found["abs_max_vin"] == 24.0


def test_crystal_frequency_maps_to_both_clock_slots() -> None:
    found = _slots("RP2040 with a 16 MHz crystal")
    assert found["clock_external"] == 16.0
    assert found["freq_mhz"] == 16.0


def test_split_rail_notation_is_read() -> None:
    assert _slots("run the core from a 1V8 supply")["supply_range"] == 1.8
    assert _slots("3V3 rail powers the MCU")["supply_range"] == 3.3


def test_capacitor_claims() -> None:
    assert _slots("use a 1uF input capacitor")["required_cin"] == 1.0
    assert _slots("470nF output capacitor")["required_cout"] == pytest.approx(0.47)


def test_cc_pulldown_claim() -> None:
    assert _slots("5.1k CC1 and CC2 pulldown resistors")["cc_pulldown_ohm"] == 5100.0


def test_quote_preserves_the_users_own_words() -> None:
    text = "Please power the STM32F103C8T6 from a 5 V rail for compatibility."
    claim = next(c for c in extract_claims(text) if "supply_range" in c.slots)
    assert claim.quote in " ".join(text.split())
    assert "5 V" in claim.quote


def test_device_hint_names_a_nearby_device() -> None:
    claim = next(
        c for c in extract_claims("power the STM32F103C8T6 from 5 V")
        if "supply_range" in c.slots
    )
    assert claim.device_hint == "STM32F103"


def test_multiple_claims_are_all_returned() -> None:
    text = (
        "Feed the AMS1117-3.3 from a 12 V input, power the STM32F103C8T6 from the "
        "resulting 3.3 V rail, and fit a 16 MHz crystal."
    )
    found = _slots(text)
    assert found["vin_range"] == 12.0
    assert found["supply_range"] == 3.3
    assert found["clock_external"] == 16.0


# --------------------------------------------------------------------------- #
# What must NOT be extracted
# --------------------------------------------------------------------------- #


def test_part_number_digits_are_not_read_as_values() -> None:
    """The masking guard. Every one of these contains a voltage-like fragment."""
    for text in (
        "use an AMS1117-3.3 regulator to supply the board",
        "fit a PESD5V0L1BA on the protected input rail",
        "an ESP32-C3FH4 supply design",
        "LP2985-3.3 powers the logic rail",
        "AP2112K-3.3TRG1 supplies the MCU",
    ):
        assert "supply_range" not in _slots(text), text
        assert "vin_range" not in _slots(text), text


def test_packages_pin_counts_and_lcsc_codes_are_not_values() -> None:
    for text in (
        "LQFP48 package, supply the MCU appropriately",
        "SOT-223 regulator input",
        "order C2765186 for the USB-C input",
        "0603 capacitors on the supply rail",
        "a 48-pin part powered sensibly",
    ):
        found = _slots(text)
        assert not found, (text, found)


def test_a_bare_number_with_no_unit_is_not_a_claim() -> None:
    """A resistance needs its unit.

    Before the unit was made mandatory, "supply the board with 5 somethings"
    produced a 5 ohm CC-pulldown claim: the bare "5" matched, and the keyword
    "rd " matched inside "boa**rd** ".
    """
    assert _slots("supply the board with 5 somethings") == {}
    assert _slots("power rail number 3") == {}
    assert _slots("fit 5 pulldown resistors") == {}


def test_a_unit_with_no_context_is_not_a_claim() -> None:
    """A number alone says nothing about which datasheet figure it concerns."""
    assert _slots("16 MHz") == {}
    assert _slots("dimensions 24 V") == {}
    assert _slots("the enclosure measures 5 mm") == {}


def test_negation_suppresses_the_claim() -> None:
    for text in (
        "supply the MCU from 3.3 V, not 5 V",
        "power it from 3.3 V rather than 5 V",
        "avoid a 5 V supply rail",
        "不要用 5V 供电",
        "供电 3.3V 而不是 5V",
    ):
        found = _slots(text)
        assert found.get("supply_range") != 5.0, (text, found)


def test_negation_keeps_the_positive_value_in_the_same_sentence() -> None:
    found = _slots("supply the MCU from 3.3 V, not 5 V")
    assert found["supply_range"] == 3.3


def test_frequency_without_a_clock_word_is_not_a_clock_claim() -> None:
    assert "clock_external" not in _slots("the board is 16 mm wide")
    assert "clock_external" not in _slots("supply 16 V to the input")


def test_empty_and_blank_text() -> None:
    assert extract_claims("") == []
    assert extract_claims("   \n\t ") == []


def test_extraction_is_deterministic() -> None:
    text = "power the STM32F103C8T6 from 5 V with a 16 MHz crystal"
    first = extract_claims(text)
    second = extract_claims(text)
    assert [c.describe() for c in first] == [c.describe() for c in second]


# --------------------------------------------------------------------------- #
# Task 10 — Tier 1 hard conflicts and acknowledgement
# --------------------------------------------------------------------------- #


def _arbitrate(text: str, devices: list[str], *, acks: str = "") -> Arbitration:
    sheets = [s for s in (fact_sheet(name) for name in devices) if s is not None]
    return arbitrate(
        extract_claims(text), sheets, acks=parse_acks(acks or text)
    )


def _by_slot(result: Arbitration, slot: str) -> ClaimVerdict:
    return next(v for v in result.verdicts if v.slot == slot)


def test_five_volts_on_an_stm32f103_is_a_cited_hard_conflict() -> None:
    """The canonical burn case: the datasheet range is 2.0-3.6 V."""
    result = _arbitrate("power the STM32F103C8T6 from 5 V", ["STM32F103"])
    verdict = _by_slot(result, "supply_range")

    assert verdict.tier == "hard"
    assert not verdict.ok
    assert verdict.severity is Severity.ERROR
    assert verdict.needs_ack
    assert "DS5319" in verdict.citation
    assert "p.38" in verdict.citation or "Table 9" in verdict.citation
    assert "DAMAGE" in verdict.message
    assert "5 V" in verdict.message
    assert result.blocking == (verdict,)


def test_twenty_four_volts_into_an_ams1117_is_a_hard_conflict() -> None:
    """The AMS1117 publishes an absolute maximum of 15 V and no recommended range.

    So the conflict must be found on ``abs_max_vin`` while ``vin_range`` reports
    no fact — which is exactly why a claim maps to both slots.
    """
    result = _arbitrate("24 V input into the AMS1117-3.3", ["AMS1117-3.3"])
    hard = _by_slot(result, "abs_max_vin")
    assert hard.tier == "hard" and not hard.ok and hard.needs_ack
    assert "15" in hard.message

    soft = _by_slot(result, "vin_range")
    assert soft.tier == "no_fact"
    assert soft.ok, "a missing figure must fail open"
    assert "missing evidence, not approval" in soft.message


def test_a_legal_value_produces_no_conflict() -> None:
    result = _arbitrate("power the STM32F103C8T6 from 3.3 V", ["STM32F103"])
    assert result.blocking == ()
    assert _by_slot(result, "supply_range").ok


def test_sixteen_megahertz_on_an_rp2040_is_a_hard_conflict() -> None:
    """The RP2040 mandates exactly 12 MHz."""
    result = _arbitrate("RP2040 board with a 16 MHz crystal", ["RP2040"])
    verdict = _by_slot(result, "clock_external")
    assert not verdict.ok and verdict.needs_ack
    assert "12" in verdict.message


def test_a_claim_with_no_matching_device_class_fails_open() -> None:
    """A supply claim with no MCU on the table cannot be judged."""
    result = _arbitrate("power the board from 5 V", ["AMS1117-3.3"])
    verdict = _by_slot(result, "supply_range")
    assert verdict.tier == "no_fact" and verdict.ok
    assert "no part of the required class is named" in verdict.message


# --------------------------------------------------------------------------- #
# The ack token
# --------------------------------------------------------------------------- #


def test_ack_token_is_scoped_to_slot_and_value() -> None:
    assert ack_token("supply_range", 5.0) == "supply_range=5"
    assert ack_token("supply_range", 5) == ack_token("supply_range", 5.00)
    assert ack_token("supply_range", 5.5) != ack_token("supply_range", 5.0)
    assert ack_token("vin_range", 5.0) != ack_token("supply_range", 5.0)


def test_parse_acks_reads_the_machine_readable_form() -> None:
    assert parse_acks("ACK-RISK: supply_range=5") == {"supply_range=5"}
    assert parse_acks("ack-risk: SUPPLY_RANGE = 5.00") == {"supply_range=5"}
    assert parse_acks("no acknowledgement here") == frozenset()


def test_an_acknowledged_conflict_stops_blocking_but_is_still_recorded() -> None:
    """Downgraded, never deleted — the audit trail is the whole point."""
    text = "power the STM32F103C8T6 from 5 V\nACK-RISK: supply_range=5"
    result = _arbitrate(text, ["STM32F103"])
    verdict = _by_slot(result, "supply_range")

    assert not verdict.ok, "the conflict is still a conflict"
    assert verdict.acknowledged
    assert not verdict.needs_ack
    assert result.blocking == ()
    assert result.accepted == (verdict,)
    assert verdict.citation, "an accepted risk must keep its citation"


def test_an_ack_for_a_different_value_does_not_apply() -> None:
    text = "power the STM32F103C8T6 from 5 V\nACK-RISK: supply_range=4"
    verdict = _by_slot(_arbitrate(text, ["STM32F103"]), "supply_range")
    assert verdict.needs_ack, "changing the value must invalidate the ack"


def test_an_ack_line_does_not_supply_context_to_a_claim() -> None:
    """Regression: the token itself used to change what a claim was about.

    "ACK-RISK: vin_range=5" contains "vin", so a "5 V" on the previous line was
    read as a REGULATOR INPUT claim and the MCU's supply range was never checked
    — the acknowledgement silently disabled the very check it was answering.
    """
    text = "power the STM32F103C8T6 from 5 V\nACK-RISK: vin_range=5"
    slots = {slot for claim in extract_claims(text) for slot in claim.slots}
    assert slots == {"supply_range"}, slots


def test_an_ack_for_a_different_slot_does_not_apply() -> None:
    text = "power the STM32F103C8T6 from 5 V\nACK-RISK: vin_range=5"
    verdict = _by_slot(_arbitrate(text, ["STM32F103"]), "supply_range")
    assert verdict.needs_ack, "an ack is not a global switch"


def test_the_message_names_the_token_the_user_must_supply() -> None:
    verdict = _by_slot(
        _arbitrate("power the STM32F103C8T6 from 5 V", ["STM32F103"]), "supply_range"
    )
    assert verdict.ack_token == "supply_range=5"


def test_no_claims_means_no_verdicts() -> None:
    result = _arbitrate("a plain two-layer board", ["STM32F103"])
    assert result.verdicts == ()
    assert result.blocking == ()


# --------------------------------------------------------------------------- #
# Task 11 — Tier 2 experience judgement
# --------------------------------------------------------------------------- #

_NO_FACT_TEXT = "10 uF input capacitor on the AMS1117-3.3"


def _unresolved() -> Arbitration:
    """An arbitration whose claim genuinely has no datasheet figure.

    The AMS1117 records ``required_cin`` as ``not_asserted``: its Applications
    Information covers only the output and adjust-pin capacitors.
    """
    result = _arbitrate(_NO_FACT_TEXT, ["AMS1117-3.3"])
    assert result.unresolved, "this fixture must exercise the no_fact path"
    return result


def test_a_value_within_normal_practice_is_adopted_and_recorded() -> None:
    """Silent adoption must still be traceable."""
    judged = judge_by_experience(
        _unresolved(),
        ask=lambda v: ExperienceOpinion(
            within_norm=True, typical_range="1-22 uF", reason="standard bulk input"
        ),
        corpus_ids=["linear_regulators", "decoupling"],
    )
    verdict = _by_slot(judged, "required_cin")
    assert verdict.tier == "advisory"
    assert verdict.ok and verdict.severity is Severity.INFO
    assert not verdict.needs_ack
    assert "1-22 uF" in verdict.message
    assert "linear_regulators" in verdict.message, "the basis must be recorded"
    assert verdict.advisory_sources == ("linear_regulators", "decoupling")


def test_a_value_outside_normal_practice_asks_the_user() -> None:
    judged = judge_by_experience(
        _unresolved(),
        ask=lambda v: ExperienceOpinion(
            within_norm=False, typical_range="1-22 uF", reason="far above practice"
        ),
    )
    verdict = _by_slot(judged, "required_cin")
    assert verdict.tier == "advisory"
    assert not verdict.ok
    assert verdict.needs_ack
    assert verdict.ack_token == ack_token("required_cin", 10.0)
    assert "EXPERIENCE, not a manual figure" in verdict.message


def test_an_advisory_verdict_is_never_an_error() -> None:
    """Without a page reference it has no standing to block a board."""
    for within in (True, False):
        judged = judge_by_experience(
            _unresolved(),
            ask=lambda v, w=within: ExperienceOpinion(within_norm=w),
        )
        for verdict in judged.verdicts:
            if verdict.tier == "advisory":
                assert verdict.severity is not Severity.ERROR, verdict


def test_an_acknowledged_advisory_stops_blocking() -> None:
    judged = judge_by_experience(
        _unresolved(),
        ask=lambda v: ExperienceOpinion(within_norm=False),
        acks=frozenset({ack_token("required_cin", 10.0)}),
    )
    verdict = _by_slot(judged, "required_cin")
    assert not verdict.ok and verdict.acknowledged and not verdict.needs_ack


def test_an_unavailable_advisor_fails_open_and_says_so() -> None:
    """Offline is a supported mode; a soft check must not be able to stop a run."""
    judged = judge_by_experience(_unresolved(), ask=lambda v: None)
    verdict = _by_slot(judged, "required_cin")
    assert verdict.tier == "no_fact"
    assert verdict.ok and not verdict.needs_ack
    assert "neither confirmed nor questioned" in verdict.message


def test_an_exploding_advisor_also_fails_open() -> None:
    def boom(verdict: ClaimVerdict) -> ExperienceOpinion:
        raise RuntimeError("model unreachable")

    judged = judge_by_experience(_unresolved(), ask=boom)
    assert all(v.ok for v in judged.verdicts)
    assert judged.blocking == ()


def test_hard_verdicts_are_left_untouched_by_the_experience_pass() -> None:
    hard = _arbitrate("power the STM32F103C8T6 from 5 V", ["STM32F103"])
    judged = judge_by_experience(
        hard, ask=lambda v: ExperienceOpinion(within_norm=True)
    )
    verdict = _by_slot(judged, "supply_range")
    assert verdict.tier == "hard"
    assert not verdict.ok and verdict.severity is Severity.ERROR


def test_the_experience_prompt_states_that_no_datasheet_figure_exists() -> None:
    verdict = _unresolved().unresolved[0]
    prompt = experience_prompt(verdict, knowledge="[linear_regulators] ...")
    assert "No datasheet in the fact base states a limit" in prompt
    assert "10 uF" in prompt
    assert "linear_regulators" in prompt


def test_the_experience_system_prompt_forbids_guessing_a_limit() -> None:
    assert "NOT quoting a datasheet" in EXPERIENCE_SYSTEM
    assert "a guess presented as a limit is worse" in EXPERIENCE_SYSTEM
