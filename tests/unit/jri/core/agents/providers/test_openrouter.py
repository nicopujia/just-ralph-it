"""Tests for the OpenRouter model provider adapter."""

from jri.core.agents.models import AgentModelConfig
from jri.core.agents.providers.openrouter import OPENROUTER_REGISTRY


def test_openrouter_cheap_preset_formats_pydantic_ai_model_ids() -> None:
    """OpenRouter presets produce Pydantic AI-compatible model IDs."""
    config = OPENROUTER_REGISTRY.load_model_config(
        preset_name="cheap",
        env={},
    )

    assert isinstance(config, AgentModelConfig)
    assert config.interviewer == "openrouter:deepseek/deepseek-chat"
    assert config.explorer == ("openrouter:qwen/qwen2.5-vl-32b-instruct")


def test_openrouter_model_formatter_preserves_explicit_prefix() -> None:
    """Already-qualified OpenRouter model strings are passed through."""
    assert OPENROUTER_REGISTRY.format_model_id(
        "openrouter:deepseek/custom"
    ) == ("openrouter:deepseek/custom")


def test_openrouter_role_overrides_replace_preset_values() -> None:
    """Role overrides are provider-specific, not global config logic."""
    config = OPENROUTER_REGISTRY.load_model_config(
        preset_name="cheap",
        env={
            "JRI_INTERVIEWER_MODEL_ID": "anthropic/claude-sonnet-4-5",
            "JRI_EXPLORER_MODEL_ID": "qwen/custom",
        },
    )

    assert config.interviewer == ("openrouter:anthropic/claude-sonnet-4-5")
    assert config.explorer == "openrouter:qwen/custom"


def test_openrouter_exposes_named_provider_registry() -> None:
    """Preset names are explicit provider-owned configuration."""
    assert OPENROUTER_REGISTRY.provider == "openrouter"
    assert set(OPENROUTER_REGISTRY.presets) == {"cheap"}
