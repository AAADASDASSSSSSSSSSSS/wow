"""The frontend half of option-shaped decisions.

Verified against the WIRE FORMAT rather than against the agent's own module: the
app talks to the service over HTTP, so a payload it cannot parse is a bug even if
the producer and the consumer would agree in-process. The payload here is written
by hand for that reason.

Deliberately does not use the shared ``mock_agent_client`` fixture: that one
depends on ``mock_env``, which clears ``os.environ`` wholesale, and Streamlit's
own config loader then fails to resolve a home directory inside the script
thread. The app under test needs no environment of its own.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from streamlit.testing.v1 import AppTest

from schema import AgentInfo, ChatMessage, ServiceMetadata
from schema.models import OpenAIModelName

_OPTION_A = "改到手册限值内（依据同上）"
_OPTION_B = "保留 5.0 V 并接受风险（会记入最终报告）"
_OPTION_C = "supply_range 换成别的数值"

_PAYLOAD = {
    "decisions": [
        {
            "slot": "supply_range",
            "question": "supply_range —— 怎么处理？",
            "kind": "risk",
            "options": [
                {
                    "key": "A",
                    "label": _OPTION_A,
                    "value": "For supply_range, the requested 5.0 V is rejected.",
                    "basis": "DS5319 Table 9 p.38",
                    "ack_token": "",
                    "free_text": False,
                },
                {
                    "key": "B",
                    "label": _OPTION_B,
                    "value": "",
                    "basis": "",
                    "ack_token": "supply_range=5",
                    "free_text": False,
                },
                {
                    "key": "C",
                    "label": _OPTION_C,
                    "value": "",
                    "basis": "",
                    "ack_token": "",
                    "free_text": True,
                },
            ],
            "recommended_key": "A",
            "citation": "DS5319 Table 9 p.38",
        }
    ]
}

_PROSE = "请注意 —— 当前需求与数据手册中的硬性限值冲突。\n\n1. supply_range —— 怎么处理？"
_MESSAGE = f"{_PROSE}\n```ratsnest-decisions\n{json.dumps(_PAYLOAD, ensure_ascii=False)}\n```"


@pytest.fixture
def mock_client():
    """A stubbed AgentClient without wiping the process environment."""
    info = ServiceMetadata(
        default_agent="ratsnestpro-multi-agent",
        agents=[AgentInfo(key="ratsnestpro-multi-agent", description="RatsNestPro")],
        default_model=OpenAIModelName.GPT_5_NANO,
        models=[OpenAIModelName.GPT_5_NANO],
    )
    with (
        patch("client.AgentClient") as factory,
        patch("voice.VoiceManager.from_env", return_value=None),
    ):
        client = factory.return_value
        client.info = info
        client.agent = "ratsnestpro-multi-agent"
        yield client


def _app_with_open_decisions() -> AppTest:
    at = AppTest.from_file("../../src/streamlit_app.py")
    at.session_state.thread_id = "decisions-thread"
    at.session_state.messages = [ChatMessage(type="ai", content=_MESSAGE)]
    at.run(timeout=30)
    return at


def _submit_button(at: AppTest):
    for button in at.button:
        if "Submit" in (button.label or ""):
            return button
    raise AssertionError(f"no submit button among {[b.label for b in at.button]}")


def test_the_options_render_as_a_radio_group(mock_client) -> None:
    at = _app_with_open_decisions()
    assert not at.exception

    assert len(at.radio) == 1, "one control per undecided item"
    assert at.radio[0].options == [
        f"A. {_OPTION_A}",
        f"B. {_OPTION_B}",
        f"C. {_OPTION_C}",
    ]


def test_the_recommended_option_is_preselected(mock_client) -> None:
    at = _app_with_open_decisions()
    assert at.radio[0].value == f"A. {_OPTION_A}"


def test_the_payload_is_not_shown_to_the_user(mock_client) -> None:
    at = _app_with_open_decisions()
    shown = "\n".join(block.value for block in at.chat_message[0].markdown)
    assert "ratsnest-decisions" not in shown
    assert "ack_token" not in shown
    assert "supply_range —— 怎么处理？" in shown, "the question itself still shows"


def test_submitting_a_choice_sends_the_canonical_token(mock_client) -> None:
    """The button is a convenience over typing, never a looser protocol."""
    at = _app_with_open_decisions()
    mock_client.ainvoke = AsyncMock(return_value=ChatMessage(type="ai", content="continuing"))

    at.sidebar.toggle[0].set_value(False)  # non-streaming keeps the assertion simple
    at.radio[0].set_value(f"B. {_OPTION_B}")
    _submit_button(at).click().run(timeout=30)

    assert not at.exception
    mock_client.ainvoke.assert_called_once()
    assert mock_client.ainvoke.call_args.kwargs["message"] == "PICK: supply_range=B"


def test_a_free_text_value_rides_along_with_the_choice(mock_client) -> None:
    at = _app_with_open_decisions()
    mock_client.ainvoke = AsyncMock(return_value=ChatMessage(type="ai", content="continuing"))

    at.sidebar.toggle[0].set_value(False)
    at.radio[0].set_value(f"C. {_OPTION_C}")
    at.text_input[0].set_value("用 3.3 V")
    _submit_button(at).click().run(timeout=30)

    assert not at.exception
    sent = mock_client.ainvoke.call_args.kwargs["message"]
    assert "PICK: supply_range=C" in sent
    assert "用 3.3 V" in sent


def test_a_message_without_options_renders_no_form(mock_client) -> None:
    at = AppTest.from_file("../../src/streamlit_app.py")
    at.session_state.thread_id = "plain-thread"
    at.session_state.messages = [ChatMessage(type="ai", content="just a normal answer")]
    at.run(timeout=30)

    assert not at.exception
    assert at.radio == []


# --------------------------------------------------------------------------- #
# The submitted reply must survive the rerun the form schedules
# --------------------------------------------------------------------------- #


def test_the_reply_is_sent_once_and_the_stash_is_cleared(mock_client) -> None:
    """The regression: the click used to open the stream itself.

    ``clear_on_submit`` schedules a rerun to reset the widgets, and that rerun
    landed while the stream opened by the same run was still iterating. Streamlit
    closed the generator mid-flight, the service saw the client vanish and
    cancelled the graph run, so the answered turn was recorded and never advanced.
    """
    at = _app_with_open_decisions()
    mock_client.ainvoke = AsyncMock(return_value=ChatMessage(type="ai", content="continuing"))

    at.sidebar.toggle[0].set_value(False)
    at.radio[0].set_value(f"A. {_OPTION_A}")
    _submit_button(at).click().run(timeout=30)

    assert not at.exception
    assert mock_client.ainvoke.call_count == 1, "one answer must produce one turn"
    assert "ratsnest_decision_reply" not in at.session_state, "the stash is consumed"


def test_a_new_question_starts_at_its_own_recommendation(mock_client) -> None:
    """``clear_on_submit`` is what stops an answer leaking into the next turn.

    Driven the way production drives it — the follow-up form arrives as the
    agent's reply — rather than by writing message state by hand, because the form
    reset is applied relative to the run that handled the click.
    """
    other = json.loads(json.dumps(_PAYLOAD))
    other["decisions"][0]["slot"] = "layer_count"
    other["decisions"][0]["question"] = "层数 —— 需求里没写。做几层板？"
    follow_up = f"prose\n```ratsnest-decisions\n{json.dumps(other, ensure_ascii=False)}\n```"

    at = _app_with_open_decisions()
    mock_client.ainvoke = AsyncMock(return_value=ChatMessage(type="ai", content=follow_up))
    at.sidebar.toggle[0].set_value(False)
    at.radio[0].set_value(f"B. {_OPTION_B}")
    _submit_button(at).click().run(timeout=30)

    assert not at.exception, "a second form in one session must not crash the app"
    assert mock_client.ainvoke.call_args.kwargs["message"] == "PICK: supply_range=B"
    assert at.radio, "the follow-up question renders its own control"
    assert at.radio[0].value == f"A. {_OPTION_A}", "the new question keeps its own default"


# --------------------------------------------------------------------------- #
# The 17-step pipeline reports progress while it runs
# --------------------------------------------------------------------------- #


def _pipeline_event(step: str, status: str, done: int) -> ChatMessage:
    """One workflow event in the wire shape tools.py writes."""
    return ChatMessage(
        type="custom",
        content="",
        custom_data={
            "kind": "workflow_event",
            "phase": f"pipeline:{step}",
            "status": status,
            "detail": "",
            "completed_steps": done,
            "total_steps": 17,
        },
    )


def test_pipeline_events_render_one_progress_bar(mock_client) -> None:
    """A streamed build must show how far along it is.

    The service was verified to deliver 34 of these events for a real build (one
    started/completed pair per step), so a run that looks hung is a rendering
    failure, not a missing signal. Seventeen separate status boxes were the old
    behaviour and conveyed no position.
    """

    async def fake_stream(**_kwargs):
        yield _pipeline_event("requirements", "started", 0)
        yield _pipeline_event("requirements", "completed", 1)
        yield _pipeline_event("selection", "started", 1)
        yield _pipeline_event("selection", "blocked", 2)
        yield ChatMessage(type="ai", content="done")

    at = _app_with_open_decisions()
    mock_client.astream = fake_stream
    at.sidebar.toggle[0].set_value(True)  # streaming on: the events only exist here
    at.radio[0].set_value(f"A. {_OPTION_A}")
    _submit_button(at).click().run(timeout=30)

    assert not at.exception
    # ``AppTest`` exposes no progress-bar accessor, so the container it lives in
    # is the observable: one box for the whole pipeline, not one per step.
    labels = [str(box.label) for box in at.status]
    assert any("制板" in label for label in labels), f"no pipeline status box among {labels}"
    assert any("2/17" in label for label in labels), "the box reports position, not just activity"
    # Survives the rerun that ends the turn only because the events are kept.
    stored = [
        m
        for m in at.session_state.messages
        if m.type == "custom" and str(m.custom_data.get("phase", "")).startswith("pipeline:")
    ]
    assert len(stored) == 4, f"progress must stay in history, found {len(stored)}"
