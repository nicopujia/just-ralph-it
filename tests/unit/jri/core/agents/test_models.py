"""Tests for shared agent model configuration."""

import pytest

from jri.core.agents.models import AgentModelPreset, ProviderModelRegistry


def test_provider_registry_resolves_preset_and_role_overrides() -> None:
    """Provider registries resolve role-specific model IDs."""
    registry = ProviderModelRegistry(
        provider="test-provider",
        api_key_env_var="TEST_PROVIDER_API_KEY",
        presets={
            "default": AgentModelPreset(
                interviewer="interviewer-default",
                explorer="explorer-default",
            )
        },
        format_model_id=lambda value: f"provider:{value}",
    )

    config = registry.load_model_config(
        preset_name="default",
        env={"JRI_EXPLORER_MODEL_ID": "explorer-override"},
    )

    assert config.interviewer == "provider:interviewer-default"
    assert config.explorer == "provider:explorer-override"


def test_provider_registry_rejects_unknown_presets() -> None:
    """Unsupported preset names are reported with valid alternatives."""
    registry = ProviderModelRegistry(
        provider="test-provider",
        api_key_env_var="TEST_PROVIDER_API_KEY",
        presets={
            "cheap": AgentModelPreset(
                interviewer="interviewer",
                explorer="explorer",
            )
        },
        format_model_id=lambda value: value,
    )

    with pytest.raises(ValueError, match="Supported presets: cheap"):
        registry.load_model_config(preset_name="expensive", env={})


def test_provider_registry_validates_credentials() -> None:
    """Provider-specific credentials are required for live model calls."""
    registry = ProviderModelRegistry(
        provider="test-provider",
        api_key_env_var="TEST_PROVIDER_API_KEY",
        presets={},
        format_model_id=lambda value: value,
    )

    registry.validate_credentials({"TEST_PROVIDER_API_KEY": "secret"})
    with pytest.raises(ValueError, match="TEST_PROVIDER_API_KEY"):
        registry.validate_credentials({})
