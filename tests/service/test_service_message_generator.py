import asyncio
import json
from unittest.mock import patch

import pytest
from langchain_core.language_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolCall
from langgraph.checkpoint.memory import MemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, MessagesState, StateGraph

from schema import ChatMessage, StreamInput


class FakeToolModel(FakeMessagesListChatModel):
    """A fake model that supports tool calls."""

    def __init__(self, responses: list[BaseMessage]):
        super().__init__(responses=responses)

    def bind_tools(self, tools, **kwargs):
        return self


@pytest.mark.asyncio
async def test_three_layer_supervisor_hierarchy_agent_with_fake_model():
    responses = [
        AIMessage(
            content="",
            tool_calls=[
                ToolCall(name="transfer_to_sub-agent-research_expert", args={}, id="call-1")
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[ToolCall(name="transfer_to_sub-agent-math_expert", args={}, id="call-2")],
        ),
        AIMessage(
            content="", tool_calls=[ToolCall(name="add", args={"a": 2, "b": 3}, id="call-3")]
        ),
        AIMessage(content="2+3 is 5"),  # This is the response from the math expert,
        AIMessage(
            content="The Maths Expert says the answer is 5."
        ),  # This is the response from the research expert
        AIMessage(content="The result is 5."),
    ]

    from agents.langgraph_supervisor_hierarchy_agent import workflow

    agent = workflow(FakeToolModel(responses)).compile(checkpointer=MemorySaver())

    with patch("service.service.get_agent", return_value=agent):
        from service.service import message_generator

        messages = []
        async for chunk in message_generator(
            StreamInput(message="Add 2 and 3"), agent_id="langgraph-supervisor-hierarchy-agent"
        ):
            if chunk and chunk.strip() != "data: [DONE]":  # Skip [DONE] message
                chat_message = json.loads(chunk.lstrip("data: "))["content"]
                messages.append(ChatMessage.model_validate(chat_message))

        for msg in messages:
            print(msg)

        assert messages[0].tool_calls[0]["name"] == "transfer_to_sub-agent-research_expert"
        assert messages[1].content == "Successfully transferred to sub-agent-research_expert"
        assert messages[2].tool_calls[0]["name"] == "transfer_to_sub-agent-math_expert"
        assert messages[3].content == "Successfully transferred to sub-agent-math_expert"
        assert messages[4].tool_calls[0]["name"] == "add"
        assert messages[5].content == "5.0"
        assert messages[6].content == "2+3 is 5"
        assert messages[7].tool_calls[0]["name"] == "transfer_back_to_supervisor-research_expert"
        assert messages[8].content == "Successfully transferred back to supervisor-research_expert"
        assert messages[9].content == "The Maths Expert says the answer is 5."
        assert messages[10].tool_calls[0]["name"] == "transfer_back_to_supervisor"
        assert messages[11].content == "Successfully transferred back to supervisor"
        assert messages[12].content == "The result is 5."


@pytest.mark.asyncio
async def test_workflow_custom_event_is_streamed_as_typed_chat_message():
    def phase_node(_state):
        get_stream_writer()(
            {
                "kind": "workflow_event",
                "phase": "architect",
                "status": "started",
            }
        )
        return {"messages": [AIMessage(content="done")]}

    builder = StateGraph(MessagesState)
    builder.add_node("phase", phase_node)
    builder.add_edge(START, "phase")
    builder.add_edge("phase", END)
    agent = builder.compile(checkpointer=MemorySaver())

    with patch("service.service.get_agent", return_value=agent):
        from service.service import message_generator

        messages: list[ChatMessage] = []
        async for chunk in message_generator(
            StreamInput(message="go", thread_id="workflow-event-thread"),
            agent_id="test-agent",
        ):
            if not chunk.startswith("data: {"):
                continue
            envelope = json.loads(chunk.removeprefix("data: "))
            if envelope["type"] == "message":
                messages.append(ChatMessage.model_validate(envelope["content"]))

    workflow_event = next(message for message in messages if message.type == "custom")
    assert workflow_event.custom_data == {
        "kind": "workflow_event",
        "phase": "architect",
        "status": "started",
    }


@pytest.mark.asyncio
async def test_same_thread_runs_are_serialized():
    from service.run_coordination import serialize_thread_run

    active = 0
    maximum_active = 0

    async def worker() -> None:
        nonlocal active, maximum_active
        async with serialize_thread_run("agent", "same-thread"):
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(worker(), worker(), worker())

    assert maximum_active == 1


@pytest.mark.asyncio
async def test_new_checkpoint_threads_are_scoped_by_agent():
    from service.service import _checkpoint_thread_id

    builder = StateGraph(MessagesState)
    builder.add_edge(START, END)
    graph = builder.compile(checkpointer=MemorySaver())

    assert (
        await _checkpoint_thread_id(graph, "agent-a", "shared-client-thread")
        == "agent-a:shared-client-thread"
    )
    assert (
        await _checkpoint_thread_id(graph, "agent-b", "shared-client-thread")
        == "agent-b:shared-client-thread"
    )


@pytest.mark.asyncio
async def test_legacy_checkpoint_thread_remains_readable():
    from service.service import _checkpoint_thread_id

    def echo_node(_state):
        return {"messages": [AIMessage(content="legacy")]}

    builder = StateGraph(MessagesState)
    builder.add_node("echo", echo_node)
    builder.add_edge(START, "echo")
    builder.add_edge("echo", END)
    graph = builder.compile(checkpointer=MemorySaver())
    await graph.ainvoke(
        {"messages": []},
        config={"configurable": {"thread_id": "legacy-thread"}},
    )

    assert (
        await _checkpoint_thread_id(graph, "agent-a", "legacy-thread")
        == "legacy-thread"
    )
