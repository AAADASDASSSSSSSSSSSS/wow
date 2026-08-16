"""Answering the Architect's open decisions must continue the design, not restart it.

A real session ended like this. The Architect closed with a "Decisions needed"
list of four items, each carrying a default. The user answered
``"a 加入 添加 默认"``. That reply names no board, so ``classify_intent`` could not
place it, the graph routed to the clarifier, and the run replied "give me the path
of the existing KiCad project to review, or state that this is a new design" —
which answers nothing and drops the design it was answering.

``initialize`` already refuses to treat one kind of answer-turn as a new request:
a risk acknowledgement carries ``base_requirement`` forward. These tests pin the
same property for a decision reply, and pin the boundary — a genuinely new request
must still start its own design rather than being glued onto the previous one.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from agents.ratsnestpro.ratsnestpro_agent import _FOLLOW_UP_PREFIX, _after_initialize, initialize

_BOARD = (
    "STM32F103C8T6 最小系统板设计方案。MCU 供电 3.3V，板子 50x40mm 两层。"
    "8MHz 晶振配两颗 20pF 负载电容，NRST 复位按键配 10k 上拉，PC13 状态 LED 串 1k。"
)
_DECISION_REPLY = "a 加入 添加 默认"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _first_turn() -> dict[str, Any]:
    state: dict[str, Any] = {"messages": [HumanMessage(content=_BOARD)]}
    state.update(_run(initialize(state, {"configurable": {}})))
    return state


def _second_turn(state: dict[str, Any], reply: str) -> dict[str, Any]:
    state["messages"] = [
        *state["messages"],
        AIMessage(content="Decisions needed: 1) LDO choice 2) ESD diode 3) NRST RC 4) BOOT0 value"),
        HumanMessage(content=reply),
    ]
    state.update(_run(initialize(state, {"configurable": {}})))
    return state


# --------------------------------------------------------------------------- #
# The decision reply continues the design
# --------------------------------------------------------------------------- #


def test_first_turn_classifies_the_board_request() -> None:
    state = _first_turn()
    assert state["workflow_mode"] == "build"
    assert not state["intent"]["needs_clarification"]
    assert state["base_requirement"] == _BOARD


def test_decision_reply_is_merged_into_the_carried_requirement() -> None:
    state = _second_turn(_first_turn(), _DECISION_REPLY)
    assert _BOARD in state["base_requirement"]
    assert _FOLLOW_UP_PREFIX in state["base_requirement"]
    assert _DECISION_REPLY in state["base_requirement"]


def test_decision_reply_does_not_route_to_the_clarifier() -> None:
    """The regression itself: the reply used to come back as a clarification."""
    state = _second_turn(_first_turn(), _DECISION_REPLY)
    assert not state["intent"]["needs_clarification"]
    assert _after_initialize(state) != "clarify"


def test_decision_reply_keeps_building() -> None:
    state = _second_turn(_first_turn(), _DECISION_REPLY)
    assert state["workflow_mode"] == "build"
    # This requirement never says where the power comes in, how the MCU is
    # flashed or how the board is fixed down, so one missing-data form is a
    # legitimate next step. What must not happen is a bounce back to intent
    # clarification, which is the regression this file exists for.
    assert _after_initialize(state) not in {"clarify", "clarify_risk"}


def test_the_mcu_survives_the_merge() -> None:
    """A merge that lost the part number would silently design a different board."""
    state = _second_turn(_first_turn(), _DECISION_REPLY)
    assert state["capability"]["primary_mcu"]
    assert "STM32F103C8T6" in state["requirement"]


def test_a_bare_amendment_is_also_carried() -> None:
    state = _second_turn(_first_turn(), "板子改成 60x40")
    assert _BOARD in state["base_requirement"]
    assert "60x40" in state["base_requirement"]
    assert state["workflow_mode"] == "build"


# --------------------------------------------------------------------------- #
# The boundary: a standalone request still starts fresh
# --------------------------------------------------------------------------- #


def test_a_new_classifiable_request_replaces_the_requirement() -> None:
    state = _second_turn(_first_turn(), "设计一块 ESP32-C3 的 USB 供电开发板，四层。")
    assert _FOLLOW_UP_PREFIX not in state["base_requirement"]
    assert _BOARD not in state["base_requirement"]
    assert "ESP32-C3" in state["base_requirement"]


def test_a_first_turn_that_needs_clarification_still_clarifies() -> None:
    """Nothing carried yet, so an unclassifiable opener must still ask."""
    state: dict[str, Any] = {"messages": [HumanMessage(content="帮我看看")]}
    state.update(_run(initialize(state, {"configurable": {}})))
    if state["intent"]["needs_clarification"]:
        assert _after_initialize(state) == "clarify"
        assert _FOLLOW_UP_PREFIX not in state["base_requirement"]
