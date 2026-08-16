import os
from unittest.mock import patch

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_groq import ChatGroq
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from core.llm import get_model
from schema.models import (
    AnthropicModelName,
    DeepseekModelName,
    EricaiModelName,
    FakeModelName,
    GroqModelName,
    OllamaModelName,
    OpenAIModelName,
)


def test_get_model_openai():
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test_key"}):
        model = get_model(OpenAIModelName.GPT_5_NANO)
        assert isinstance(model, ChatOpenAI)
        assert model.model_name == "gpt-5-nano"
        assert model.streaming is True


def test_get_model_deepseek():
    get_model.cache_clear()
    with (
        patch("core.llm.settings.DEEPSEEK_API_KEY", SecretStr("test_key")),
        patch("core.llm.settings.DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    ):
        model = get_model(DeepseekModelName.DEEPSEEK_V4_FLASH)

    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "deepseek-v4-flash"
    assert model.openai_api_base == "https://api.deepseek.com"
    assert model.openai_api_key == SecretStr("test_key")
    assert model.streaming is True
    assert model.request_timeout == 60
    assert model.max_retries == 3
    assert model.extra_body == {"thinking": {"type": "disabled"}}


def test_get_model_deepseek_requires_api_key():
    get_model.cache_clear()
    with patch("core.llm.settings.DEEPSEEK_API_KEY", None):
        with pytest.raises(ValueError, match="DeepSeek API key must be configured"):
            get_model(DeepseekModelName.DEEPSEEK_V4_PRO)


def test_get_model_anthropic():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test_key"}):
        model = get_model(AnthropicModelName.HAIKU_45)
        assert isinstance(model, ChatAnthropic)
        assert model.model == "claude-haiku-4-5"
        assert model.temperature == 0.5
        assert model.streaming is True


def test_get_model_anthropic_sonnet_5_omits_temperature():
    # Claude Sonnet 5 rejects non-default sampling parameters with a 400 error,
    # so get_model must not pass temperature for this model.
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test_key"}):
        model = get_model(AnthropicModelName.SONNET_5)
        assert isinstance(model, ChatAnthropic)
        assert model.model == "claude-sonnet-5"
        assert model.streaming is True


def test_get_model_groq():
    with patch.dict(os.environ, {"GROQ_API_KEY": "test_key"}):
        model = get_model(GroqModelName.LLAMA_31_8B)
        assert isinstance(model, ChatGroq)
        assert model.model_name == "llama-3.1-8b-instant"
        assert model.temperature == 0.5


def test_get_model_groq_guard():
    with patch.dict(os.environ, {"GROQ_API_KEY": "test_key"}):
        model = get_model(GroqModelName.GPT_OSS_SAFEGUARD_20B)
        assert isinstance(model, ChatGroq)
        assert model.model_name == "openai/gpt-oss-safeguard-20b"
        assert model.temperature < 0.01


def test_get_model_groq_gpt_oss():
    with patch.dict(os.environ, {"GROQ_API_KEY": "test_key"}):
        model = get_model(GroqModelName.GPT_OSS_120B)
        assert isinstance(model, ChatGroq)
        assert model.model_name == "openai/gpt-oss-120b"
        # Only the safeguard variant gets the temperature=0.0 override.
        assert model.temperature == 0.5


def test_get_model_ollama():
    with patch("core.settings.settings.OLLAMA_MODEL", "llama3.3"):
        model = get_model(OllamaModelName.OLLAMA_GENERIC)
        assert isinstance(model, ChatOllama)
        assert model.model == "llama3.3"
        assert model.temperature == 0.5


def test_get_model_ericai_injects_client(monkeypatch):
    # `ericai` is a private Ericsson package not installed in CI. Inject a fake
    # module so the EricAI branch can build a ChatOpenAI with the injected
    # OpenAI-subclass clients (no static API key needed).
    import sys
    import types
    from unittest.mock import MagicMock

    fake_ericai = types.ModuleType("ericai")
    sync_client = MagicMock(name="EricAI")
    async_client = MagicMock(name="AsyncEricAI")
    fake_ericai.EricAI = MagicMock(return_value=sync_client)
    fake_ericai.AsyncEricAI = MagicMock(return_value=async_client)
    monkeypatch.setitem(sys.modules, "ericai", fake_ericai)

    get_model.cache_clear()
    try:
        model = get_model(EricaiModelName.GPT_OSS_120B)
        assert isinstance(model, ChatOpenAI)
        # Unique routing id is mapped to the real gateway model id.
        assert model.model_name == "openai/gpt-oss-120b"
        assert model.streaming is True
        # Injected clients are reused instead of a static-key OpenAI client.
        assert model.client is sync_client.chat.completions
        assert model.async_client is async_client.chat.completions
        fake_ericai.EricAI.assert_called_once()
        fake_ericai.AsyncEricAI.assert_called_once()
    finally:
        get_model.cache_clear()


def test_get_model_ericai_missing_package(monkeypatch):
    import sys

    # Simulate ericai not being installed: importing it raises ImportError.
    monkeypatch.setitem(sys.modules, "ericai", None)
    get_model.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="ericai"):
            get_model(EricaiModelName.GPT_OSS_20B)
    finally:
        get_model.cache_clear()


def test_get_model_fake():
    model = get_model(FakeModelName.FAKE)
    assert isinstance(model, FakeListChatModel)
    assert model.responses == ["This is a test response from the fake model."]


def test_get_model_invalid():
    with pytest.raises(ValueError, match="Unsupported model:"):
        # Using type: ignore since we're intentionally testing invalid input
        get_model("invalid_model")  # type: ignore


def test_get_model_without_configured_provider_has_clear_error():
    get_model.cache_clear()
    with pytest.raises(ValueError, match="No LLM provider is configured"):
        get_model(None)
