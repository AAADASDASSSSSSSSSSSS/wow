"""Undecided data asked as options rather than as open questions.

Two properties carry the design and are tested hardest:

* a pick is only honoured when the slot AND the option key were actually offered,
  whether the pick arrived as a token, as shorthand, or via a model mapping prose
  onto keys — the same "model proposes, code decides" rule as ``ACK-RISK``;
* choosing the "accept the risk" option produces exactly the token the free-text
  acknowledgement produced, so one mechanism decides and the other only presents.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from agents.ratsnestpro import decisions as dec
from agents.ratsnestpro.ratsnestpro_agent import (
    _after_initialize,
    _after_parts,
    _arbitrate_requirement,
    _selected_from_reply,
    _split_assumptions,
    clarify,
    clarify_missing_data,
    clarify_open_decisions,
    clarify_risk,
    final_report,
    initialize,
)

_DANGEROUS = "Design an STM32F103C8T6 board and power the MCU from 5 V."
_TOKEN = "supply_range=5"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _blocking_verdict() -> dict[str, Any]:
    verdict = _arbitrate_requirement(_DANGEROUS).blocking[0]
    return {
        "slot": verdict.slot,
        "tier": "hard",
        "ok": False,
        "value": verdict.claim.value,
        "unit": verdict.claim.unit,
        "quote": verdict.claim.quote,
        "device": verdict.device,
        "citation": verdict.citation,
        "message": verdict.message,
        "ack_token": verdict.ack_token,
        "acknowledged": False,
    }


# --------------------------------------------------------------------------- #
# Building the option set
# --------------------------------------------------------------------------- #


def test_a_blocking_conflict_becomes_three_exclusive_options() -> None:
    [decision] = dec.risk_decisions([_blocking_verdict()], "en")
    assert decision.slot == "supply_range"
    assert [option.key for option in decision.options] == ["A", "B", "C"]
    assert decision.recommended_key == "A"


def test_the_accept_option_carries_the_existing_ack_token() -> None:
    """One waiver mechanism. The options present it; they do not replace it."""
    [decision] = dec.risk_decisions([_blocking_verdict()], "en")
    assert decision.option("B").ack_token == _TOKEN
    assert decision.option("A").ack_token == ""


def test_the_limit_option_states_a_source_and_invents_no_number() -> None:
    verdict = _blocking_verdict()
    [decision] = dec.risk_decisions([verdict], "en")
    limit = decision.option("A")
    assert verdict["citation"] in limit.basis
    assert verdict["citation"] in limit.value
    # The rejected value is named; no replacement value is fabricated here.
    assert "5.0" in limit.value
    assert "rejected" in limit.value


def test_an_acknowledged_or_passing_verdict_asks_nothing() -> None:
    passing = {**_blocking_verdict(), "ok": True}
    acknowledged = {**_blocking_verdict(), "acknowledged": True}
    assert dec.risk_decisions([passing, acknowledged], "en") == []


def test_the_routing_question_becomes_options() -> None:
    [decision] = dec.intent_decisions(
        {"needs_clarification": True, "clarification": "review or new build?"}, "en"
    )
    assert decision.slot == "task_kind"
    assert decision.question == "review or new build?"
    assert decision.option("B").free_text, "a review needs a path only the user has"
    assert dec.intent_decisions({"needs_clarification": False}, "en") == []


# --------------------------------------------------------------------------- #
# Reading an answer — structural validation, no charity
# --------------------------------------------------------------------------- #


def _decisions() -> list[dec.OpenDecision]:
    return dec.risk_decisions([_blocking_verdict()], "en")


def test_a_token_answer_needs_no_model() -> None:
    assert dec.parse_picks("PICK: supply_range=B", _decisions()) == {"supply_range": "B"}


def test_shorthand_answers_by_item_number() -> None:
    assert dec.parse_picks("1B", _decisions()) == {"supply_range": "B"}
    assert dec.parse_picks("1) a", _decisions()) == {"supply_range": "A"}


def test_a_slot_that_was_never_offered_is_dropped() -> None:
    assert dec.parse_picks("PICK: abs_max_vin=B", _decisions()) == {}


def test_an_option_key_that_was_never_offered_is_dropped() -> None:
    assert dec.parse_picks("PICK: supply_range=Z", _decisions()) == {}


def test_a_dimension_is_not_mistaken_for_a_shorthand_pick() -> None:
    """The load-bearing guard on the shorthand: only real items and keys count."""
    assert dec.parse_picks("make the board 60x40 mm", _decisions()) == {}
    assert dec.parse_picks("use 2 layers", _decisions()) == {}


def test_an_explicit_token_wins_over_shorthand_noise() -> None:
    picks = dec.parse_picks("PICK: supply_range=B (see item 1a)", _decisions())
    assert picks == {"supply_range": "B"}


def test_a_free_text_option_without_a_value_answers_nothing() -> None:
    resolved, unresolved = dec.resolve(_decisions(), {"supply_range": "C"}, reply="1C")
    assert resolved == []
    assert unresolved == ["supply_range"]


def test_a_free_text_option_keeps_what_the_user_typed() -> None:
    resolved, unresolved = dec.resolve(
        _decisions(), {"supply_range": "C"}, reply="1C use 3.3 V instead"
    )
    assert unresolved == []
    assert "3.3 V instead" in resolved[0]["value"]


def test_resolving_reports_what_is_still_open() -> None:
    resolved, unresolved = dec.resolve(_decisions(), {})
    assert resolved == []
    assert unresolved == ["supply_range"]


def test_accepted_tokens_come_only_from_the_chosen_option() -> None:
    resolved, _ = dec.resolve(_decisions(), {"supply_range": "B"})
    assert dec.accepted_tokens(resolved) == {_TOKEN}
    resolved, _ = dec.resolve(_decisions(), {"supply_range": "A"})
    assert dec.accepted_tokens(resolved) == set()


def test_a_settled_decision_is_appended_to_the_requirement() -> None:
    resolved, _ = dec.resolve(_decisions(), {"supply_range": "A"})
    merged = dec.apply_decisions(_DANGEROUS, resolved)
    assert merged.startswith(_DANGEROUS)
    assert f"{dec.DECISION_PREFIX} supply_range=A" in merged
    assert dec.apply_decisions(_DANGEROUS, []) == _DANGEROUS


# --------------------------------------------------------------------------- #
# Rendering and the wire format
# --------------------------------------------------------------------------- #


def test_every_option_is_rendered_with_its_own_token() -> None:
    text = dec.render(_decisions(), "en")
    for key in ("A", "B", "C"):
        assert dec.pick_token("supply_range", key) in text
    assert "recommended" in text


def test_the_rendering_follows_the_reply_language() -> None:
    text = dec.render(dec.risk_decisions([_blocking_verdict()], "zh"), "zh")
    assert "推荐" in text
    assert dec.pick_token("supply_range", "B") in text, "tokens stay verbatim"


def test_the_payload_round_trips_and_is_separable_from_the_prose() -> None:
    offered = _decisions()
    message = dec.render(offered, "en") + "\n" + dec.payload_block(offered)
    prose, payload = dec.split_payload(message)
    assert dec.PAYLOAD_FENCE not in prose
    assert [d["slot"] for d in payload] == ["supply_range"]
    assert dec.from_state(payload)[0].option("B").ack_token == _TOKEN


def test_a_malformed_payload_leaves_the_message_intact() -> None:
    broken = f"question\n```{dec.PAYLOAD_FENCE}\n{{not json}}\n```"
    prose, payload = dec.split_payload(broken)
    assert payload == []
    assert prose == broken


def test_from_state_drops_corrupt_records() -> None:
    assert dec.from_state([{"nonsense": 1}, "text", None]) == []
    assert dec.from_state("not a list") == []


# --------------------------------------------------------------------------- #
# The graph: asking, then continuing
# --------------------------------------------------------------------------- #


def test_the_risk_message_offers_options_and_keeps_the_ack_token() -> None:
    state = {
        "messages": [HumanMessage(content=_DANGEROUS)],
        "reply_language": "en",
        "claim_verdicts": [_blocking_verdict()],
    }
    result = _run(clarify_risk(state))
    text = result["messages"][0].content

    assert "STOP" in text, "the existing warning is not weakened"
    assert f"ACK-RISK: {_TOKEN}" in text
    assert dec.pick_token("supply_range", "B") in text
    assert [d["slot"] for d in result["open_decisions"]] == ["supply_range"]


def test_the_clarify_node_offers_the_task_kind_options() -> None:
    state = {
        "messages": [HumanMessage(content="check my board")],
        "reply_language": "en",
        "intent": {"needs_clarification": True, "clarification": "review or build?"},
    }
    result = _run(clarify(state))
    text = result["messages"][0].content
    assert "review or build?" in text
    assert dec.pick_token("task_kind", "A") in text
    assert [d["slot"] for d in result["open_decisions"]] == ["task_kind"]


def test_picking_the_accept_option_unblocks_the_build_without_a_model() -> None:
    offered = dec.to_state(dec.risk_decisions([_blocking_verdict()], "en"))
    state = {
        "messages": [
            HumanMessage(content=_DANGEROUS),
            AIMessage(content="STOP — ..."),
            HumanMessage(content="PICK: supply_range=B"),
        ],
        "base_requirement": _DANGEROUS,
        "pending_acks": [_TOKEN],
        "accepted_acks": [],
        "open_decisions": offered,
    }
    result = _run(initialize(state, {}))

    assert "STM32F103C8T6" in result["requirement"], "the design survives the answer"
    assert f"ACK-RISK: {_TOKEN}" in result["requirement"]
    assert result["accepted_acks"] == [_TOKEN]
    assert result["pending_acks"] == []
    assert result["open_decisions"] == [], "a consumed menu is cleared"
    assert [r["slot"] for r in result["resolved_decisions"]] == ["supply_range"]
    # The point of the whole mechanism: answering resumes the work instead of
    # ending another turn on the same question. This requirement states no board
    # size, layer count or clock source, so the missing-data form legitimately
    # comes next; what must never happen is the settled risk being re-asked.
    assert _after_initialize(result) != "clarify_risk"


def test_picking_the_limit_option_keeps_the_conflict_unwaived() -> None:
    """Choosing "stay inside the limit" must NOT emit an acknowledgement."""
    offered = dec.to_state(dec.risk_decisions([_blocking_verdict()], "en"))
    state = {
        "messages": [
            HumanMessage(content=_DANGEROUS),
            AIMessage(content="STOP — ..."),
            HumanMessage(content="1A"),
        ],
        "base_requirement": _DANGEROUS,
        "pending_acks": [_TOKEN],
        "accepted_acks": [],
        "open_decisions": offered,
    }
    result = _run(initialize(state, {}))

    assert result["accepted_acks"] == []
    assert f"{dec.DECISION_PREFIX} supply_range=A" in result["requirement"]
    assert [r["slot"] for r in result["resolved_decisions"]] == ["supply_range"]


def test_a_new_request_discards_the_stale_menu(monkeypatch) -> None:
    import agents.ratsnestpro.ratsnestpro_agent as agent

    monkeypatch.setattr(agent, "get_model", lambda _name: _FakeModel('{"picks": {}}'))
    offered = dec.to_state(dec.risk_decisions([_blocking_verdict()], "en"))
    state = {
        "messages": [
            HumanMessage(content=_DANGEROUS),
            AIMessage(content="STOP — ..."),
            HumanMessage(content="Forget that. Build an RP2040 board instead."),
        ],
        "base_requirement": _DANGEROUS,
        "pending_acks": [_TOKEN],
        "accepted_acks": [],
        "open_decisions": offered,
        "resolved_decisions": [{"slot": "supply_range", "key": "A", "value": "x"}],
    }
    result = _run(initialize(state, {}))

    assert "RP2040" in result["base_requirement"]
    assert result["resolved_decisions"] == [], "answers belonged to the old request"


def test_a_model_may_map_prose_but_only_onto_offered_keys(monkeypatch) -> None:
    import agents.ratsnestpro.ratsnestpro_agent as agent

    monkeypatch.setattr(
        agent,
        "get_model",
        lambda _name: _FakeModel(json.dumps({"picks": {"supply_range": "B", "abs_max_vin": "B"}})),
    )
    resolved, still_open = _selected_from_reply("I know the risk, keep 5 V", _decisions(), {})
    assert [r["slot"] for r in resolved] == ["supply_range"]
    assert still_open == []
    assert dec.accepted_tokens(resolved) == {_TOKEN}


def test_an_invented_option_key_from_a_model_is_discarded(monkeypatch) -> None:
    import agents.ratsnestpro.ratsnestpro_agent as agent

    monkeypatch.setattr(
        agent, "get_model", lambda _name: _FakeModel('{"picks": {"supply_range": "Z"}}')
    )
    resolved, still_open = _selected_from_reply("do whatever", _decisions(), {})
    assert resolved == []
    assert [decision.slot for decision in still_open] == ["supply_range"]


def test_a_broken_model_picks_nothing(monkeypatch) -> None:
    import agents.ratsnestpro.ratsnestpro_agent as agent

    class _Boom:
        def with_config(self, **_kwargs: Any) -> _Boom:
            return self

        def invoke(self, _messages: Any) -> Any:
            raise RuntimeError("model unreachable")

    monkeypatch.setattr(agent, "get_model", lambda _name: _Boom())
    resolved, still_open = _selected_from_reply("yes go ahead", _decisions(), {})
    assert resolved == []
    assert [decision.slot for decision in still_open] == ["supply_range"]


class _FakeModel:
    def __init__(self, content: str) -> None:
        self._content = content

    def with_config(self, **_kwargs: Any) -> _FakeModel:
        return self

    def invoke(self, _messages: Any) -> AIMessage:
        return AIMessage(content=self._content)


# --------------------------------------------------------------------------- #
# The report knows what it did not know
# --------------------------------------------------------------------------- #


def _report(state_extra: dict[str, Any], language: str = "en") -> str:
    state = {
        "workflow_mode": "build",
        "reply_language": language,
        "trace": [],
        "intent": {},
        "hardware": {"release_ready": False, "release_blockers": [], "actual_files": []},
        "claim_verdicts": [],
        **state_extra,
    }
    return final_report(state)["messages"][0].content


def test_the_report_lists_what_the_user_settled() -> None:
    resolved, _ = dec.resolve(_decisions(), {"supply_range": "A"})
    report = _report({"resolved_decisions": resolved})
    assert "Data ledger" in report
    assert "Settled by you" in report
    assert "supply_range" in report


def test_the_report_lists_what_is_still_undecided() -> None:
    report = _report({"open_decisions": dec.to_state(_decisions())})
    assert "Still undecided" in report
    assert "supply_range" in report


def test_no_decisions_means_no_ledger_section() -> None:
    assert "Data ledger" not in _report({})


def test_the_ledger_is_localized() -> None:
    resolved, _ = dec.resolve(
        dec.risk_decisions([_blocking_verdict()], "zh"), {"supply_range": "A"}
    )
    report = _report({"resolved_decisions": resolved}, language="zh")
    assert "数据清单" in report
    assert "你已选定" in report

# --------------------------------------------------------------------------- #
# Assumed values offered for confirmation before the board is built
# --------------------------------------------------------------------------- #


_ASSUMPTIONS = [
    {
        "slot": "tvs_part",
        "question": "tvs_part —— 需求里没写，我先按 SMAJ24A 处理。",
        "assumed": "SMAJ24A",
        "basis": "candidate_by_step",
        "alternatives": ["SMBJ26A"],
    },
    {
        "slot": "crystal_load_pf",
        "assumed": "20 pF",
        "basis": "engineering default",
    },
]


def _assumption_state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "workflow_mode": "build",
        "parts": {"status": "unavailable"},
        "architecture": {"assumptions": _ASSUMPTIONS},
        "resolved_decisions": [],
        "reply_language": "zh",
        "messages": [],
        "trace": [],
    }
    state.update(overrides)
    return state


def test_an_assumed_value_is_the_first_option() -> None:
    offered = dec.assumption_decisions(_ASSUMPTIONS, "zh")
    first = offered[0]
    assert first.slot == "tvs_part"
    assert first.kind == "assumption"
    assert first.recommended_key == "A"
    assert "SMAJ24A" in first.options[0].label
    assert "SMAJ24A" in first.options[0].value


def test_alternatives_become_their_own_options_and_free_text_is_last() -> None:
    offered = dec.assumption_decisions(_ASSUMPTIONS, "zh")
    keys = [option.key for option in offered[0].options]
    assert keys == ["A", "B", "C"]
    assert "SMBJ26A" in offered[0].options[1].value
    assert offered[0].options[-1].free_text is True
    # No alternatives: the escape hatch still follows the assumed value directly.
    assert [option.key for option in offered[1].options] == ["A", "B"]
    assert offered[1].options[-1].free_text is True


def test_an_entry_without_an_assumed_value_is_dropped_not_asked() -> None:
    assert dec.assumption_decisions([{"slot": "board_thickness"}], "en") == []
    assert dec.assumption_decisions([{"assumed": "1.6 mm"}], "en") == []


def test_a_slot_the_user_already_settled_is_not_asked_again() -> None:
    offered = dec.assumption_decisions(_ASSUMPTIONS, "zh", settled=frozenset({"tvs_part"}))
    assert [decision.slot for decision in offered] == ["crystal_load_pf"]


def test_the_confirmation_gate_runs_before_any_board_file_exists() -> None:
    assert _after_parts(_assumption_state()) == "clarify_missing_data"


def test_the_gate_asks_once_and_then_builds() -> None:
    assert _after_parts(_assumption_state(missing_data_asked=True)) == "hardware_phase"


def test_no_assumptions_means_no_extra_turn() -> None:
    assert _after_parts(_assumption_state(architecture={"assumptions": []})) == "hardware_phase"


def test_a_review_is_never_gated_on_assumptions() -> None:
    assert _after_parts(_assumption_state(workflow_mode="review")) == "final_report"


def test_the_gate_message_carries_options_a_frontend_can_render() -> None:
    result = _run(clarify_missing_data(_assumption_state()))
    assert result["missing_data_asked"] is True
    content = result["messages"][0].content
    prose, payload = dec.split_payload(content)
    assert [item["slot"] for item in payload] == ["tvs_part", "crystal_load_pf"]
    assert dec.PICK_PREFIX in prose


def test_a_partial_reply_reasks_only_the_unanswered_items() -> None:
    requirement = "This is a new KiCad build task for a passive two-layer connector board."
    offered = dec.to_state(dec.assumption_decisions(_ASSUMPTIONS, "zh"))
    state = {
        "messages": [HumanMessage(content="PICK: tvs_part=A")],
        "base_requirement": requirement,
        "open_decisions": offered,
        "resolved_decisions": [],
        "pending_acks": [],
        "accepted_acks": [],
        "missing_data_asked": True,
        "reply_language": "zh",
    }

    result = _run(initialize(state, {}))

    assert [item["slot"] for item in result["resolved_decisions"]] == ["tvs_part"]
    assert [item["slot"] for item in result["open_decisions"]] == ["crystal_load_pf"]
    assert result["missing_data_asked"] is False
    assert _after_initialize(result) == "clarify_open_decisions"

    prompt = _run(clarify_open_decisions(result))
    _, payload = dec.split_payload(prompt["messages"][0].content)
    assert [item["slot"] for item in payload] == ["crystal_load_pf"]
    assert prompt["missing_data_asked"] is True


def test_submitting_the_menu_unchanged_keeps_the_assumed_values() -> None:
    offered = dec.assumption_decisions(_ASSUMPTIONS, "zh")
    picks = dec.parse_picks("PICK: tvs_part=A\nPICK: crystal_load_pf=A", offered)
    resolved, unresolved = dec.resolve(offered, picks)
    assert unresolved == []
    assert "SMAJ24A" in dec.apply_decisions("req", resolved)
    assert "20 pF" in dec.apply_decisions("req", resolved)


def test_choosing_an_alternative_overrides_the_assumption() -> None:
    offered = dec.assumption_decisions(_ASSUMPTIONS, "zh")
    resolved, _ = dec.resolve(offered, dec.parse_picks("PICK: tvs_part=B", offered))
    appended = dec.apply_decisions("req", resolved)
    assert "SMBJ26A" in appended
    assert "SMAJ24A" not in appended


def test_the_architect_block_is_parsed_and_stripped_from_the_prose() -> None:
    text = (
        "Design basis prose.\n\n"
        "```ratsnest-assumptions\n"
        '{"assumptions": [{"slot": "tvs_part", "assumed": "SMAJ24A"}]}\n'
        "```"
    )
    prose, items = _split_assumptions(text)
    assert items == [{"slot": "tvs_part", "assumed": "SMAJ24A"}]
    assert "ratsnest-assumptions" not in prose
    assert prose == "Design basis prose."


def test_a_malformed_architect_block_costs_the_prompt_not_the_prose() -> None:
    text = "Prose.\n```ratsnest-assumptions\n{not json}\n```"
    prose, items = _split_assumptions(text)
    assert items == []
    assert prose == text
