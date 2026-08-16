from typing import Any

from langchain_core.messages import ChatMessage
from langgraph.types import StreamWriter
from pydantic import BaseModel, Field

# Canned notices shared by the assistant-style agents. They never reach the
# model, so they are translated here instead of being pinned by a system prompt;
# `agents.language.localized` picks the entry for the detected reply language and
# falls back to English.
SAFETY_NOTICE: dict[str, str] = {
    "en": "This conversation was flagged for unsafe content: {categories}",
    "zh": "本次对话被标记为不安全内容:{categories}",
}

OUT_OF_STEPS_NOTICE: dict[str, str] = {
    "en": "Sorry, need more steps to process this request.",
    "zh": "抱歉,处理这个请求需要更多步骤。",
}


class CustomData(BaseModel):
    "Custom data being sent by an agent"

    data: dict[str, Any] = Field(description="The custom data")

    def to_langchain(self) -> ChatMessage:
        return ChatMessage(content=[self.data], role="custom")

    def dispatch(self, writer: StreamWriter) -> None:
        writer(self.to_langchain())
