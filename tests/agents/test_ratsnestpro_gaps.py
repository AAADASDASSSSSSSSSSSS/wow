"""Missing data raised by a code reading of the requirement, with no model.

The failure this covers was silent: the Architect's ``ratsnest-assumptions``
block is model output, so a gateway timeout left ``architecture.assumptions``
empty, the router found nothing undecided and the run built a board without
asking anything. Two properties matter most here:

* a requirement that states a slot is never asked about it — a fully specified
  requirement must still run start to finish without a single question;
* a gap survives every degraded dependency, because nothing in this path calls a
  model, a search provider or a parts cache.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import HumanMessage

from agents.ratsnestpro import decisions as dec
from agents.ratsnestpro.gaps import GAPS, merge_assumptions, requirement_gaps
from agents.ratsnestpro.ratsnestpro_agent import (
    _after_initialize,
    _pending_assumption_decisions,
    clarify_missing_data,
    initialize,
)

# Under-specified on purpose: names the MCU, the power source and the debug port,
# and leaves size, layers, rail, clock, LED resistor and mounting unsaid.
_SPARSE = (
    "用 RP2040 做一块小板子，USB 供电，要一颗状态 LED、一个 BOOTSEL 按键、"
    "一个 SWD 调试口，外挂 QSPI Flash。真的用 KiCad 出原理图和 PCB、真的布通线、"
    "真的生成 Gerber 和 BOM。"
)

# The cases/ baseline: every slot this module knows about is stated.
_COMPLETE = (
    "做一块 50×40 mm 的两层 PCB：STM32F103C8T6 做主控，USB-C 接口只用来取 5V 供电，"
    "经 AMS1117-3.3 降到 3.3V。外挂 8MHz 晶振配两颗 20pF 负载电容，一个 10k 上拉的 "
    "NRST 复位按键，一颗状态 LED 接 PC13 并串 1k 限流电阻，留一个 4 pin SWD 调试排针。"
    "板子四角各一个 M2 安装孔。"
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _slots(requirement: str, language: str = "zh") -> list[str]:
    return [item["slot"] for item in requirement_gaps(requirement, language)]


# --------------------------------------------------------------------------- #
# What counts as a gap
# --------------------------------------------------------------------------- #


def test_a_sparse_requirement_raises_the_slots_it_omits() -> None:
    slots = _slots(_SPARSE)
    assert "board_outline" in slots, "no dimensions anywhere in the text"
    assert "layer_count" in slots
    assert "main_rail" in slots, "no logic voltage stated"
    assert "clock_source" in slots
    assert "led_resistor" in slots, "an LED is named with no series resistor"
    assert "mounting" in slots


def test_a_sparse_requirement_does_not_re_ask_what_it_states() -> None:
    slots = _slots(_SPARSE)
    assert "input_power" not in slots, "'USB 供电' settles where power comes from"
    assert "debug_port" not in slots, "'SWD 调试口' settles programming"


def test_a_complete_requirement_raises_nothing() -> None:
    """The non-regression that keeps this from becoming an interrogation."""
    assert requirement_gaps(_COMPLETE, "zh") == []


def test_an_empty_requirement_raises_nothing() -> None:
    assert requirement_gaps("", "zh") == []
    assert requirement_gaps("   ", "en") == []


def test_a_slot_is_only_relevant_when_the_board_has_the_part() -> None:
    """No LED on the board, no question about its resistor."""
    assert "led_resistor" not in _slots("做一块 RP2040 板子，USB 供电，SWD 调试口。")


def test_settled_slots_are_not_raised_again() -> None:
    settled = frozenset({"board_outline", "layer_count"})
    slots = [item["slot"] for item in requirement_gaps(_SPARSE, "zh", settled=settled)]
    assert "board_outline" not in slots
    assert "layer_count" not in slots
    assert slots, "the rest of the gaps still stand"


def test_appended_runtime_evidence_cannot_suppress_a_question() -> None:
    """Fact sheets mention voltages the user never asked for.

    Reading those as user intent silenced the rail question on every run that
    reached the Architect, which is the opposite of the point.
    """
    polluted = (
        _SPARSE + "\n\nGROUNDED ARCHITECT EVIDENCE\nVDD 3.3 V nominal, 2 layer stackup, M2 holes"
    )
    slots = _slots(polluted)
    assert "main_rail" in slots
    assert "layer_count" in slots


# --------------------------------------------------------------------------- #
# Shape of the record
# --------------------------------------------------------------------------- #


def test_every_gap_carries_a_value_and_a_basis() -> None:
    """``assumption_decisions`` drops an entry with no assumed value."""
    for record in requirement_gaps(_SPARSE, "zh"):
        assert record["assumed"].strip(), record["slot"]
        assert record["basis"].strip(), record["slot"]
        assert record["question"].strip(), record["slot"]


def test_the_clock_option_delegates_the_frequency_instead_of_inventing_one() -> None:
    """A number without provenance is what the project forbids."""
    clock = next(gap for gap in GAPS if gap.slot == "clock_source")
    assert "datasheet" in clock.assumed.lower()
    assert not any(char.isdigit() for char in clock.assumed.split("32.768")[0])


def test_questions_follow_the_reply_language() -> None:
    zh = {item["slot"]: item["question"] for item in requirement_gaps(_SPARSE, "zh")}
    en = {item["slot"]: item["question"] for item in requirement_gaps(_SPARSE, "en")}
    assert zh["board_outline"] != en["board_outline"]
    assert "外形" in zh["board_outline"]
    assert "outline" in en["board_outline"].lower()


# --------------------------------------------------------------------------- #
# Merging with what the Architect recorded
# --------------------------------------------------------------------------- #


def test_a_recorded_assumption_wins_over_a_derived_one() -> None:
    recorded = [{"slot": "main_rail", "assumed": "1.8 V", "basis": "RP2040 §2.9"}]
    derived = [{"slot": "main_rail", "assumed": "3.3 V", "basis": "default"}]
    merged = merge_assumptions(recorded, derived)
    assert len(merged) == 1
    assert merged[0]["basis"] == "RP2040 §2.9", "the cited record survives"


def test_derived_gaps_the_architect_missed_are_kept() -> None:
    recorded = [{"slot": "tvs_part", "assumed": "PESD5V0L1BA", "basis": "candidate list"}]
    derived = [{"slot": "mounting", "assumed": "four M2 holes", "basis": "convention"}]
    merged = merge_assumptions(recorded, derived)
    assert [item["slot"] for item in merged] == ["tvs_part", "mounting"]


def test_malformed_entries_are_dropped() -> None:
    assert merge_assumptions(["not a dict"], [{"slot": "", "assumed": "x"}]) == []  # type: ignore[list-item]


# --------------------------------------------------------------------------- #
# The graph asks, with no model reachable
# --------------------------------------------------------------------------- #


def _sparse_state() -> dict[str, Any]:
    state: dict[str, Any] = {"messages": [HumanMessage(content=_SPARSE)]}
    state.update(_run(initialize(state, {"configurable": {}})))
    return state


def test_the_router_asks_before_the_architect_spends_a_model_call() -> None:
    state = _sparse_state()
    assert state["workflow_mode"] == "build"
    assert not state["architecture"].get("assumptions") if "architecture" in state else True
    assert _after_initialize(state) == "clarify_missing_data"


def test_a_complete_requirement_never_reaches_the_missing_data_form() -> None:
    """This one routes to the risk gate first — the 5 V rail next to an STM32 is a
    documented conflict — which is exactly why the assertion is about the form
    NOT being raised rather than about which node runs."""
    state: dict[str, Any] = {"messages": [HumanMessage(content=_COMPLETE)]}
    state.update(_run(initialize(state, {"configurable": {}})))
    assert _after_initialize(state) != "clarify_missing_data"
    assert _pending_assumption_decisions(state) == []


def test_the_run_asks_at_most_once() -> None:
    state = _sparse_state()
    state["missing_data_asked"] = True
    assert _after_initialize(state) != "clarify_missing_data"


def test_decisions_exist_with_an_empty_architect_record() -> None:
    """The dead-gateway case: no recorded assumptions, questions all the same."""
    state = _sparse_state()
    state["architecture"] = {"status": "blocked", "assumptions": []}
    offered = _pending_assumption_decisions(state)
    assert offered, "a failed Architect must not silence the form"
    for decision in offered:
        assert decision.kind == "assumption"
        assert decision.recommended_key == "A"
        assert decision.option("A") is not None


def test_the_form_renders_a_payload_the_frontend_can_bind() -> None:
    state = _sparse_state()
    state["architecture"] = {"status": "blocked", "assumptions": []}
    result = _run(clarify_missing_data(state))
    content = result["messages"][0].content
    prose, payload = dec.split_payload(content)
    assert payload, "the radio-button data must survive the round trip"
    assert prose.strip()
    assert result["missing_data_asked"] is True
    assert [d["slot"] for d in result["open_decisions"]] == [d["slot"] for d in payload]


def test_submitting_the_form_unchanged_keeps_the_default() -> None:
    """Option A is what the run would have used, so an unchanged submit is a no-op."""
    state = _sparse_state()
    state["architecture"] = {"status": "blocked", "assumptions": []}
    offered = _pending_assumption_decisions(state)
    reply = " ".join(dec.pick_token(decision.slot, "A") for decision in offered)
    picks = dec.parse_picks(reply, offered)
    resolved, unresolved = dec.resolve(offered, picks, reply=reply)
    assert unresolved == []
    assert len(resolved) == len(offered)
    for record, decision in zip(resolved, offered, strict=True):
        assert record["value"] == decision.option("A").value  # type: ignore[union-attr]
