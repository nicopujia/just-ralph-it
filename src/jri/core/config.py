"""Runtime configuration for JRI."""

from collections.abc import Mapping
from dataclasses import dataclass

from jri.core.agents.models import AgentModelConfig
from jri.core.agents.providers import load_provider_registries

DEFAULT_MODEL_PROVIDER = "openrouter"
DEFAULT_MODEL_PRESET = "cheap"


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """Runtime provider and model selection for JRI agents."""

    model_provider: str
    model_preset: str
    models: AgentModelConfig


class ConfigError(RuntimeError):
    """Raised when runtime configuration is invalid."""


def load_agent_runtime_config(env: Mapping[str, str]) -> AgentRuntimeConfig:
    """Load provider-agnostic agent runtime configuration."""
    provider = _env_value(env, "JRI_MODEL_PROVIDER", DEFAULT_MODEL_PROVIDER)
    preset = _env_value(env, "JRI_MODEL_PRESET", DEFAULT_MODEL_PRESET)
    registries = load_provider_registries()

    try:
        registry = registries[provider]
    except KeyError as exc:
        names = ", ".join(sorted(registries))
        msg = (
            "Unsupported JRI_MODEL_PROVIDER "
            f"{provider!r}. Supported providers: {names}."
        )
        raise ConfigError(msg) from exc
    try:
        models = registry.load_model_config(preset_name=preset, env=env)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    return AgentRuntimeConfig(
        model_provider=provider,
        model_preset=preset,
        models=models,
    )


def validate_agent_runtime_credentials(
    config: AgentRuntimeConfig,
    env: Mapping[str, str],
) -> None:
    """Validate credentials for the selected model provider."""
    registries = load_provider_registries()
    try:
        registry = registries[config.model_provider]
    except KeyError as exc:
        msg = f"Unsupported JRI_MODEL_PROVIDER {config.model_provider!r}."
        raise ConfigError(msg) from exc
    try:
        registry.validate_credentials(env)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _env_value(
    env: Mapping[str, str],
    name: str,
    default: str,
) -> str:
    value = env.get(name, default).strip()
    return value or default
