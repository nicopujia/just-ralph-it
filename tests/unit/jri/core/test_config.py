"""Tests for provider-agnostic runtime configuration."""

from pathlib import Path
from typing import cast

import pytest

from jri.core.agents import providers
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
    assert config.models.explorer == "openrouter:qwen/qwen3-vl-32b-instruct"


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


def test_runtime_config_discovers_provider_registry_from_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New provider modules work without editing global config maps."""
    provider_dir = tmp_path / "providers"
    provider_dir.mkdir()
    (provider_dir / "acme.py").write_text(
        """
from jri.core.agents.models import AgentModelPreset, ProviderModelRegistry


def format_acme_model(model_id: str) -> str:
    return f"acme:{model_id}"


ACME_REGISTRY = ProviderModelRegistry(
    provider="acme",
    api_key_env_var="ACME_API_KEY",
    presets={
        "small": AgentModelPreset(interviewer="interview", explorer="explore"),
    },
    format_model_id=format_acme_model,
)
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        providers,
        "__path__",
        [*cast("list[str]", vars(providers)["__path__"]), str(provider_dir)],
    )

    config = load_agent_runtime_config({
        "JRI_MODEL_PROVIDER": "acme",
        "JRI_MODEL_PRESET": "small",
    })

    assert config.model_provider == "acme"
    assert config.models.interviewer == "acme:interview"
    assert config.models.explorer == "acme:explore"


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
