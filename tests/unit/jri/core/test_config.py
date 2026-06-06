"""Tests for provider-agnostic runtime configuration."""

from pathlib import Path

import pytest

from jri.core.agents.models import AgentModelConfig
from jri.core.config import (
    AgentRuntimeConfig,
    ConfigError,
    load_agent_runtime_config,
    validate_agent_runtime_credentials,
)


def test_runtime_config_uses_default_provider_and_preset() -> None:
    """Runtime config chooses a provider before resolving model strings."""
    config = load_agent_runtime_config({})

    assert config.model_provider == "openrouter"
    assert config.model_preset == "cheap"
    assert isinstance(config.models, AgentModelConfig)
    assert config.models.interviewer == "openrouter:deepseek/deepseek-chat"
    assert config.models.explorer == (
        "openrouter:qwen/qwen2.5-vl-32b-instruct"
    )


def test_runtime_config_accepts_role_model_id_overrides() -> None:
    """Role-specific model IDs override the selected provider preset."""
    config = load_agent_runtime_config({
        "JRI_MODEL_PROVIDER": "openrouter",
        "JRI_MODEL_PRESET": "cheap",
        "JRI_INTERVIEWER_MODEL_ID": "anthropic/claude-sonnet-4-5",
        "JRI_EXPLORER_MODEL_ID": "qwen/custom",
    })

    assert config.models.interviewer == (
        "openrouter:anthropic/claude-sonnet-4-5"
    )
    assert config.models.explorer == "openrouter:qwen/custom"


def test_runtime_config_rejects_unsupported_provider() -> None:
    """Unsupported providers fail at config load with a useful error."""
    with pytest.raises(ConfigError, match="JRI_MODEL_PROVIDER"):
        load_agent_runtime_config({"JRI_MODEL_PROVIDER": "unsupported"})


def test_runtime_credentials_reject_unknown_provider_config() -> None:
    """Credential validation fails if runtime config is inconsistent."""
    with pytest.raises(ConfigError, match="unsupported"):
        validate_agent_runtime_credentials(
            AgentRuntimeConfig(
                model_provider="unsupported",
                model_preset="cheap",
                models=AgentModelConfig("test", "test"),
            ),
            {},
        )


def test_runtime_config_rejects_unknown_provider_preset() -> None:
    """Provider preset names are validated before model construction."""
    with pytest.raises(ConfigError, match="JRI_MODEL_PRESET"):
        load_agent_runtime_config({
            "JRI_MODEL_PROVIDER": "openrouter",
            "JRI_MODEL_PRESET": "unknown",
        })


def test_example_env_documents_runtime_env_vars() -> None:
    """The root example env file documents runtime configuration knobs."""
    content = Path("example.env").read_text(encoding="utf-8")

    for name in (
        "OPENROUTER_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "JRI_MODEL_PROVIDER",
        "JRI_MODEL_PRESET",
        "JRI_INTERVIEWER_MODEL_ID",
        "JRI_EXPLORER_MODEL_ID",
        "JRI_INTERVIEWER_FACTORY",
    ):
        assert name in content

    assert "JRI_EXPLORER_VISION_MODEL_ID" not in content
