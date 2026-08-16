from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.func import entrypoint

from agents.language import reply_language_directive
from core import get_model, settings


@entrypoint()
async def chatbot(
    inputs: dict[str, list[BaseMessage]],
    *,
    previous: dict[str, list[BaseMessage]],
    config: RunnableConfig,
):
    messages = inputs["messages"]
    if previous:
        messages = previous["messages"] + messages

    model = get_model(config["configurable"].get("model", settings.DEFAULT_MODEL))
    # Prepended per call and kept out of the saved history: the language clause is
    # derived from the latest user turn, so a stale copy would fight the current one.
    directive = SystemMessage(content=reply_language_directive(messages, config))
    response = await model.ainvoke([directive, *messages])
    return entrypoint.final(
        value={"messages": [response]}, save={"messages": messages + [response]}
    )
