"""Tasks 13-14 — asking the user before building a board the datasheet forbids.

The conversation half of the risk mechanism. Two properties carry the safety
argument and are tested hardest:

* a natural-language reply is turned into a token by a MODEL but validated by
  CODE against the tokens actually offered, so a confident or over-generous
  answer cannot waive a limit the user was never shown;
* an accepted risk appears in the final report with the page it overrode.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from ratsnestpro.eda.factclaim import ACK_PREFIX

from agents.ratsnestpro.ratsnestpro_agent import (
    _accepted_from_reply,
    _after_initialize,
    _arbitrate_requirement,
    _with_acks,
    clarify_risk,
    final_report,
    initialize,
    ratsnestpro_multi_agent,
)

_DANGEROUS = "Design an STM32F103C8T6 board and power the MCU from 5 V."
_TOKEN = "supply_range=5"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# The graph
# --------------------------------------------------------------------------- #


def test_clarify_risk_is_a_node_of_the_graph() -> None:
    assert "clarify_risk" in set(ratsnestpro_multi_agent.get_graph().nodes)


def test_a_blocking_conflict_routes_to_clarify_risk_not_to_architect() -> None:
    state = {
        "workflow_mode": "build",
        "intent": {},
        "pending_acks": [_TOKEN],
    }
    assert _after_initialize(state) == "clarify_risk"


def test_an_acknowledged_conflict_lets_the_build_proceed() -> None:
    state = {"workflow_mode": "build", "intent": {}, "pending_acks": []}
    assert _after_initialize(state) == "architect_phase"


def test_only_a_build_is_gated_by_risk() -> None:
    """A review or a parts lookup cannot damage anything by proceeding."""
    for mode, expected in (
        ("review", "reviewer_phase"),
        ("parts", "parts_phase"),
        ("research", "architect_phase"),
    ):
        state = {"workflow_mode": mode, "intent": {}, "pending_acks": [_TOKEN]}
        assert _after_initialize(state) == expected, mode


def test_intent_clarification_still_takes_precedence() -> None:
    state = {
        "workflow_mode": "build",
        "intent": {"needs_clarification": True},
        "pending_acks": [_TOKEN],
    }
    assert _after_initialize(state) == "clarify"


# --------------------------------------------------------------------------- #
# Arbitration in the agent layer
# --------------------------------------------------------------------------- #


def test_arbitration_finds_the_conflict_and_names_the_token() -> None:
    arbitration = _arbitrate_requirement(_DANGEROUS)
    assert len(arbitration.blocking) == 1
    verdict = arbitration.blocking[0]
    assert verdict.slot == "supply_range"
    assert verdict.ack_token == _TOKEN
    assert verdict.citation


def test_arbitration_runs_tier_one_only_and_needs_no_model() -> None:
    """Deterministic by construction: the experience tier belongs to the pipeline."""
    arbitration = _arbitrate_requirement(_DANGEROUS)
    assert all(v.tier in {"hard", "no_fact"} for v in arbitration.verdicts)


def test_an_acknowledged_requirement_has_nothing_blocking() -> None:
    text = f"{_DANGEROUS}\n{ACK_PREFIX} {_TOKEN}"
    assert _arbitrate_requirement(text).blocking == ()


def test_with_acks_appends_the_transport_lines() -> None:
    merged = _with_acks(_DANGEROUS, {_TOKEN})
    assert merged.startswith(_DANGEROUS)
    assert f"{ACK_PREFIX} {_TOKEN}" in merged
    assert _with_acks(_DANGEROUS, set()) == _DANGEROUS


# --------------------------------------------------------------------------- #
# The warning message
# --------------------------------------------------------------------------- #


def test_the_risk_message_states_the_value_the_source_and_the_token() -> None:
    verdict = _arbitrate_requirement(_DANGEROUS).blocking[0]
    state = {
        "messages": [HumanMessage(content=_DANGEROUS)],
        "reply_language": "en",
        "claim_verdicts": [
            {
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
        ],
    }
    text = _run(clarify_risk(state))["messages"][0].content

    assert "STOP" in text
    assert "damage" in text.lower()
    assert "DS5319" in text, text
    assert f"{ACK_PREFIX} {_TOKEN}" in text
    # The "change the value" way out is still offered, now as a pickable option
    # rather than as an open-ended invitation to reply in prose.
    assert "Stay inside the documented limit" in text
    assert "PICK: supply_range=A" in text


def test_the_risk_message_ends_on_the_options_not_on_an_open_invitation() -> None:
    """No "reply in your own words": a free-text ending solicits an answer that
    cannot be validated against what was actually offered."""
    verdict = _arbitrate_requirement(_DANGEROUS).blocking[0]
    state = {
        "messages": [HumanMessage(content=_DANGEROUS)],
        "reply_language": "en",
        "claim_verdicts": [
            {
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
        ],
    }
    text = _run(clarify_risk(state))["messages"][0].content

    assert "Choose one:" not in text
    assert "reply that you accept the risk" not in text
    assert "To accept:" not in text, "the token is data, not an instruction"
    assert "Risk token:" in text


def test_the_risk_message_follows_the_reply_language() -> None:
    state = {
        "messages": [HumanMessage(content="给 STM32F103C8T6 供电 5V")],
        "reply_language": "zh",
        "claim_verdicts": [
            {
                "slot": "supply_range",
                "tier": "hard",
                "ok": False,
                "value": 5.0,
                "unit": "V",
                "quote": "供电 5V",
                "device": "STM32F103",
                "citation": "DS5319 Table 9",
                "message": "conflict",
                "ack_token": _TOKEN,
                "acknowledged": False,
            }
        ],
    }
    text = _run(clarify_risk(state))["messages"][0].content
    assert "数据手册" in text
    assert f"{ACK_PREFIX} {_TOKEN}" in text, "the token stays verbatim"


# --------------------------------------------------------------------------- #
# Turning a reply into an acknowledgement — model proposes, code decides
# --------------------------------------------------------------------------- #


def test_an_explicit_token_in_the_reply_needs_no_model() -> None:
    accepted = _accepted_from_reply(f"{ACK_PREFIX} {_TOKEN}", [_TOKEN], {})
    assert accepted == {_TOKEN}


def test_a_natural_reply_is_mapped_by_the_model(monkeypatch) -> None:
    import agents.ratsnestpro.ratsnestpro_agent as agent

    monkeypatch.setattr(
        agent,
        "get_model",
        lambda _name: _FakeModel('{"accepted": ["supply_range=5"]}'),
    )
    accepted = _accepted_from_reply("I know the risk, use 5 V anyway", [_TOKEN], {})
    assert accepted == {_TOKEN}


def test_a_token_that_was_never_offered_is_discarded(monkeypatch) -> None:
    """The load-bearing validation: a model cannot waive an unasked risk."""
    import agents.ratsnestpro.ratsnestpro_agent as agent

    monkeypatch.setattr(
        agent,
        "get_model",
        lambda _name: _FakeModel('{"accepted": ["abs_max_vin=24", "supply_range=5"]}'),
    )
    accepted = _accepted_from_reply("fine, proceed", [_TOKEN], {})
    assert accepted == {_TOKEN}, "only the offered token may be accepted"


def test_an_ambiguous_or_refusing_reply_accepts_nothing(monkeypatch) -> None:
    import agents.ratsnestpro.ratsnestpro_agent as agent

    monkeypatch.setattr(agent, "get_model", lambda _name: _FakeModel('{"accepted": []}'))
    assert _accepted_from_reply("what happens if I do?", [_TOKEN], {}) == set()


def test_a_broken_model_accepts_nothing(monkeypatch) -> None:
    """Fail closed: an unreadable answer means the question gets asked again."""
    import agents.ratsnestpro.ratsnestpro_agent as agent

    class _Boom:
        def with_config(self, **_kwargs: Any) -> _Boom:
            return self

        def invoke(self, _messages: Any) -> Any:
            raise RuntimeError("model unreachable")

    monkeypatch.setattr(agent, "get_model", lambda _name: _Boom())
    assert _accepted_from_reply("yes go ahead", [_TOKEN], {}) == set()


def test_garbage_json_accepts_nothing(monkeypatch) -> None:
    import agents.ratsnestpro.ratsnestpro_agent as agent

    monkeypatch.setattr(agent, "get_model", lambda _name: _FakeModel("not json"))
    assert _accepted_from_reply("yes", [_TOKEN], {}) == set()


def test_nothing_pending_means_nothing_can_be_accepted() -> None:
    assert _accepted_from_reply(f"{ACK_PREFIX} {_TOKEN}", [], {}) == set()


class _FakeModel:
    def __init__(self, content: str) -> None:
        self._content = content

    def with_config(self, **_kwargs: Any) -> _FakeModel:
        return self

    def invoke(self, _messages: Any) -> AIMessage:
        return AIMessage(content=self._content)


# --------------------------------------------------------------------------- #
# initialize: the second turn answers the first
# --------------------------------------------------------------------------- #


def test_initialize_records_the_pending_token_on_the_first_turn() -> None:
    result = _run(initialize({"messages": [HumanMessage(content=_DANGEROUS)]}, {}))
    assert result["pending_acks"] == [_TOKEN]
    assert result["base_requirement"] == _DANGEROUS
    assert result["accepted_acks"] == []
    assert any(v["slot"] == "supply_range" for v in result["claim_verdicts"])


def test_initialize_merges_an_acceptance_with_the_earlier_request(monkeypatch) -> None:
    """The reply must not replace the design it was answering."""
    import agents.ratsnestpro.ratsnestpro_agent as agent

    monkeypatch.setattr(
        agent,
        "get_model",
        lambda _name: _FakeModel('{"accepted": ["supply_range=5"]}'),
    )
    state = {
        "messages": [
            HumanMessage(content=_DANGEROUS),
            AIMessage(content="STOP — ..."),
            HumanMessage(content="I accept the risk, keep 5 V"),
        ],
        "base_requirement": _DANGEROUS,
        "pending_acks": [_TOKEN],
        "accepted_acks": [],
    }
    result = _run(initialize(state, {}))

    assert "STM32F103C8T6" in result["requirement"], "the design survives the reply"
    assert f"{ACK_PREFIX} {_TOKEN}" in result["requirement"]
    assert result["accepted_acks"] == [_TOKEN]
    assert result["pending_acks"] == [], "nothing is still blocking"


def test_a_new_request_while_a_risk_is_pending_is_treated_as_new(monkeypatch) -> None:
    import agents.ratsnestpro.ratsnestpro_agent as agent

    monkeypatch.setattr(agent, "get_model", lambda _name: _FakeModel('{"accepted": []}'))
    state = {
        "messages": [
            HumanMessage(content=_DANGEROUS),
            AIMessage(content="STOP — ..."),
            HumanMessage(content="Forget that. Build an RP2040 board instead."),
        ],
        "base_requirement": _DANGEROUS,
        "pending_acks": [_TOKEN],
        "accepted_acks": [],
    }
    result = _run(initialize(state, {}))
    assert "RP2040" in result["base_requirement"]
    assert "STM32F103C8T6" not in result["requirement"]
    assert result["accepted_acks"] == []


def test_a_safe_requirement_has_nothing_pending() -> None:
    safe = "Design an STM32F103C8T6 board and power the MCU from 3.3 V."
    result = _run(initialize({"messages": [HumanMessage(content=safe)]}, {}))
    assert result["pending_acks"] == []


# --------------------------------------------------------------------------- #
# Task 14 — the report section
# --------------------------------------------------------------------------- #


def _report(verdicts: list[dict[str, Any]], language: str = "en") -> str:
    state = {
        "workflow_mode": "build",
        "reply_language": language,
        "trace": [],
        "intent": {},
        "hardware": {"release_ready": False, "release_blockers": [], "actual_files": []},
        "claim_verdicts": verdicts,
    }
    return final_report(state)["messages"][0].content


_ACCEPTED = {
    "slot": "supply_range",
    "tier": "hard",
    "ok": False,
    "acknowledged": True,
    "value": 5.0,
    "unit": "V",
    "quote": "power the MCU from 5 V",
    "device": "STM32F103",
    "citation": "STM32F103x8/xB DS5319 / Table 9 p.38",
    "message": "supply_range: you asked for 5 V but the limit is 2-3.6 V",
    "ack_token": _TOKEN,
}


def test_an_accepted_risk_is_reported_with_its_source_and_token() -> None:
    report = _report([_ACCEPTED])
    assert "Accepted risks" in report
    assert "NOT resolved problems" in report
    assert "5.0 V" in report
    assert "STM32F103" in report
    assert "DS5319" in report and "p.38" in report
    assert f"{ACK_PREFIX} {_TOKEN}" in report


def test_no_accepted_risk_means_no_section() -> None:
    assert "Accepted risks" not in _report([])
    unacknowledged = {**_ACCEPTED, "acknowledged": False}
    assert "Accepted risks" not in _report([unacknowledged])


def test_an_advisory_finding_is_reported_separately_from_a_datasheet_one() -> None:
    """Experience and a cited page must not read as the same kind of statement."""
    advisory = {
        "slot": "required_cin",
        "tier": "advisory",
        "ok": False,
        "acknowledged": False,
        "value": 10.0,
        "unit": "uF",
        "quote": "10 uF",
        "device": "AMS1117-3.3",
        "citation": "",
        "message": "required_cin: outside normal practice. This is EXPERIENCE.",
        "ack_token": "required_cin=10",
    }
    report = _report([_ACCEPTED, advisory])
    assert "Advisory findings" in report
    assert "engineering experience only" in report
    assert report.index("Accepted risks") < report.index("Advisory findings")


def test_the_report_section_is_localized() -> None:
    report = _report([_ACCEPTED], language="zh")
    assert "已接受的风险" in report
    assert "DS5319" in report, "citations stay verbatim"
    assert f"{ACK_PREFIX} {_TOKEN}" in report
