import json
import logging
import os
from unittest.mock import patch

import pytest
from pydantic import SecretStr, ValidationError

from core.settings import LogLevel, Settings, check_str_is_http
from schema.models import (
    AnthropicModelName,
    AzureOpenAIModelName,
    DeepseekModelName,
    EricaiModelName,
    FakeModelName,
    OpenAIModelName,
    VertexAIModelName,
)


def test_check_str_is_http():
    # Test valid HTTP URLs
    assert check_str_is_http("http://example.com/") == "http://example.com/"
    assert check_str_is_http("https://api.test.com/") == "https://api.test.com/"

    # Test invalid URLs
    with pytest.raises(ValidationError):
        check_str_is_http("not_a_url")
    with pytest.raises(ValidationError):
        check_str_is_http("ftp://invalid.com")


def test_settings_default_values():
    settings = Settings(_env_file=None)
    assert settings.HOST == "0.0.0.0"
    assert settings.PORT == 8080
    assert settings.USE_AWS_BEDROCK is False
    assert settings.USE_FAKE_MODEL is False


def test_settings_no_api_keys():
    # Configuration can be inspected or validated before a provider is added.
    # The model factory, not Settings construction, owns provider requirements.
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(_env_file=None)
    assert settings.DEFAULT_MODEL is None
    assert settings.AVAILABLE_MODELS == set()


def test_settings_with_openai_key():
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test_key"}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.OPENAI_API_KEY == SecretStr("test_key")
        assert settings.DEFAULT_MODEL == OpenAIModelName.GPT_5_NANO
        assert settings.AVAILABLE_MODELS == set(OpenAIModelName)


def test_settings_with_deepseek_key():
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test_key"}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.DEEPSEEK_API_KEY == SecretStr("test_key")
        assert settings.DEEPSEEK_BASE_URL == "https://api.deepseek.com"
        assert settings.DEFAULT_MODEL == DeepseekModelName.DEEPSEEK_V4_FLASH
        assert settings.AVAILABLE_MODELS == set(DeepseekModelName)


def test_settings_with_deepseek_base_url():
    with patch.dict(
        os.environ,
        {
            "DEEPSEEK_API_KEY": "test_key",
            "DEEPSEEK_BASE_URL": "https://deepseek-proxy.example.com/v1",
        },
        clear=True,
    ):
        settings = Settings(_env_file=None)
        assert settings.DEEPSEEK_BASE_URL == "https://deepseek-proxy.example.com/v1"


def test_settings_with_anthropic_key():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test_key"}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.ANTHROPIC_API_KEY == SecretStr("test_key")
        assert settings.DEFAULT_MODEL == AnthropicModelName.HAIKU_45
        assert settings.AVAILABLE_MODELS == set(AnthropicModelName)


def test_settings_with_vertexai_credentials_file():
    with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": "test_key"}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.GOOGLE_APPLICATION_CREDENTIALS == SecretStr("test_key")
        assert settings.DEFAULT_MODEL == VertexAIModelName.GEMINI_36_FLASH
        assert settings.AVAILABLE_MODELS == set(VertexAIModelName)


def test_settings_with_multiple_api_keys():
    with patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "test_openai_key",
            "ANTHROPIC_API_KEY": "test_anthropic_key",
        },
        clear=True,
    ):
        settings = Settings(_env_file=None)
        assert settings.OPENAI_API_KEY == SecretStr("test_openai_key")
        assert settings.ANTHROPIC_API_KEY == SecretStr("test_anthropic_key")
        # When multiple providers are available, OpenAI should be the default
        assert settings.DEFAULT_MODEL == OpenAIModelName.GPT_5_NANO
        # Available models should include exactly all OpenAI and Anthropic models
        expected_models = set(OpenAIModelName)
        expected_models.update(set(AnthropicModelName))
        assert settings.AVAILABLE_MODELS == expected_models


def test_settings_with_ericai():
    with patch.dict(os.environ, {"USE_ERICAI": "true"}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.USE_ERICAI is True
        assert settings.DEFAULT_MODEL == EricaiModelName.GPT_OSS_120B
        assert settings.AVAILABLE_MODELS == set(EricaiModelName)


def test_settings_ericai_is_default_over_other_providers():
    # USE_ERICAI is checked first, so it wins the default when combined with other
    # real provider keys (an explicit DEFAULT_MODEL still overrides).
    with patch.dict(
        os.environ,
        {"USE_ERICAI": "true", "OPENAI_API_KEY": "test_openai_key"},
        clear=True,
    ):
        settings = Settings(_env_file=None)
        assert settings.DEFAULT_MODEL == EricaiModelName.GPT_OSS_120B
        assert settings.AVAILABLE_MODELS == set(EricaiModelName) | set(OpenAIModelName)


def test_settings_use_fake_model():
    with patch.dict(os.environ, {"USE_FAKE_MODEL": "true"}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.DEFAULT_MODEL == FakeModelName.FAKE
        assert settings.AVAILABLE_MODELS == set(FakeModelName)


def test_settings_use_fake_model_wins_over_ambient_real_keys():
    # USE_FAKE_MODEL must win the default even when real provider keys are present.
    with patch.dict(
        os.environ,
        {
            "USE_FAKE_MODEL": "true",
            "OPENAI_API_KEY": "test_openai_key",
            "GROQ_API_KEY": "test_groq_key",
            "GOOGLE_API_KEY": "test_google_key",
        },
        clear=True,
    ):
        settings = Settings(_env_file=None)
        assert settings.DEFAULT_MODEL == FakeModelName.FAKE
        # AVAILABLE_MODELS still unions every active provider.
        assert set(FakeModelName).issubset(settings.AVAILABLE_MODELS)
        assert set(OpenAIModelName).issubset(settings.AVAILABLE_MODELS)


def test_settings_explicit_default_model_overrides_fake():
    # An explicit DEFAULT_MODEL takes precedence over USE_FAKE_MODEL.
    with patch.dict(
        os.environ,
        {"USE_FAKE_MODEL": "true", "OPENAI_API_KEY": "test_openai_key"},
        clear=True,
    ):
        settings = Settings(_env_file=None, DEFAULT_MODEL=OpenAIModelName.GPT_5_NANO)
        assert settings.DEFAULT_MODEL == OpenAIModelName.GPT_5_NANO


def test_settings_base_url():
    settings = Settings(HOST="0.0.0.0", PORT=8000, _env_file=None)
    assert settings.BASE_URL == "http://0.0.0.0:8000"


def test_settings_is_dev():
    settings = Settings(MODE="dev", _env_file=None)
    assert settings.is_dev() is True

    settings = Settings(MODE="prod", _env_file=None)
    assert settings.is_dev() is False


def test_settings_with_azure_openai_key():
    with patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_API_KEY": "test_key",
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT_MAP": '{"gpt-5": "deployment-1", "gpt-5-mini": "deployment-2"}',
        },
        clear=True,
    ):
        settings = Settings(_env_file=None)
        assert settings.AZURE_OPENAI_API_KEY.get_secret_value() == "test_key"
        assert settings.DEFAULT_MODEL == AzureOpenAIModelName.AZURE_GPT_5_MINI
        assert settings.AVAILABLE_MODELS == set(AzureOpenAIModelName)


def test_settings_with_both_openai_and_azure():
    with patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "test_openai_key",
            "AZURE_OPENAI_API_KEY": "test_azure_key",
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT_MAP": '{"gpt-5": "deployment-1", "gpt-5-mini": "deployment-2"}',
        },
        clear=True,
    ):
        settings = Settings(_env_file=None)
        assert settings.OPENAI_API_KEY == SecretStr("test_openai_key")
        assert settings.AZURE_OPENAI_API_KEY == SecretStr("test_azure_key")
        # When multiple providers are available, OpenAI should be the default
        assert settings.DEFAULT_MODEL == OpenAIModelName.GPT_5_NANO
        # Available models should include both OpenAI and Azure OpenAI models
        expected_models = set(OpenAIModelName)
        expected_models.update(set(AzureOpenAIModelName))
        assert settings.AVAILABLE_MODELS == expected_models


def test_settings_azure_deployment_names():
    # Delete this test
    pass


def test_settings_azure_missing_deployment_names():
    with patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_API_KEY": "test_key",
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
        },
        clear=True,
    ):
        with pytest.raises(ValidationError, match="AZURE_OPENAI_DEPLOYMENT_MAP must be set"):
            Settings(_env_file=None)


def test_settings_azure_deployment_map():
    with patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_API_KEY": "test_key",
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT_MAP": '{"gpt-5": "deploy1", "gpt-5-mini": "deploy2"}',
        },
        clear=True,
    ):
        settings = Settings(_env_file=None)
        assert settings.AZURE_OPENAI_DEPLOYMENT_MAP == {
            "gpt-5": "deploy1",
            "gpt-5-mini": "deploy2",
        }


def test_settings_azure_invalid_deployment_map():
    with patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_API_KEY": "test_key",
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT_MAP": '{"gpt-5": "deploy1"}',  # Missing required model
        },
        clear=True,
    ):
        with pytest.raises(ValueError, match="Missing required Azure deployments"):
            Settings(_env_file=None)


def test_settings_azure_openai():
    """Test Azure OpenAI settings."""
    deployment_map = {"gpt-5": "deployment1", "gpt-5-mini": "deployment2"}
    with patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_API_KEY": "test-key",
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT_MAP": json.dumps(deployment_map),
        },
    ):
        settings = Settings(_env_file=None)
        assert settings.AZURE_OPENAI_API_KEY.get_secret_value() == "test-key"
        assert settings.AZURE_OPENAI_ENDPOINT == "https://test.openai.azure.com"
        assert settings.AZURE_OPENAI_DEPLOYMENT_MAP == deployment_map


def test_log_level_enum():
    """Test LogLevel enum and its conversion to logging levels."""
    assert LogLevel.DEBUG.to_logging_level() == logging.DEBUG
    assert LogLevel.INFO.to_logging_level() == logging.INFO
    assert LogLevel.WARNING.to_logging_level() == logging.WARNING
    assert LogLevel.ERROR.to_logging_level() == logging.ERROR
    assert LogLevel.CRITICAL.to_logging_level() == logging.CRITICAL


def test_settings_log_level_default():
    """Test that LOG_LEVEL defaults to WARNING."""
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test_key"}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.LOG_LEVEL == LogLevel.WARNING
        assert settings.LOG_LEVEL.to_logging_level() == logging.WARNING


def test_settings_log_level_from_env():
    """Test that LOG_LEVEL can be set from environment variable."""
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test_key", "LOG_LEVEL": "DEBUG"}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.LOG_LEVEL == LogLevel.DEBUG
        assert settings.LOG_LEVEL.to_logging_level() == logging.DEBUG


def test_settings_log_level_invalid():
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test_key", "LOG_LEVEL": "INVALID"}, clear=True):
        with pytest.raises(ValueError, match="validation error for Settings\nLOG_LEVEL\n"):
            Settings(_env_file=None)
