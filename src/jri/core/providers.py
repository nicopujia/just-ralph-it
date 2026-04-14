import json
from importlib.resources import files
from typing import Literal, cast

from .errors import JriError

ProviderMode = Literal["chat", "start"]

_CHAT_AGENT_NAMES = {
    "model": "interrogator",
    "validator_model": "interrogator-validator",
    "explore_model": "explore",
}

_START_AGENT_NAMES = {
    "model": "ralph",
    "validator_model": "ralph-validator",
    "general_model": "general",
    "explore_model": "explore",
}

_PROVIDER_CHOICES = ("default", "openai")


def provider_choices() -> tuple[str, ...]:
    return _PROVIDER_CHOICES


def resolve_provider_models(
    provider: str | None,
    *,
    mode: ProviderMode,
    overrides: dict[str, str | None],
) -> dict[str, str | None]:
    resolved: dict[str, str | None] = {
        name: value for name, value in _provider_models(provider, mode=mode).items()
    }
    for name, value in overrides.items():
        if value is not None:
            resolved[name] = value
        elif name not in resolved:
            resolved[name] = None
    return resolved


def _provider_models(provider: str | None, *, mode: ProviderMode) -> dict[str, str]:
    if provider is None:
        return {}
    if provider in _PROVIDER_CHOICES:
        return _configured_provider_models(provider, mode=mode)
    raise JriError(f"unknown provider '{provider}'")


def _configured_provider_models(provider: str, *, mode: ProviderMode) -> dict[str, str]:
    provider_config = _load_provider_config()
    provider_data = provider_config.get(provider)
    if not isinstance(provider_data, dict):
        raise JriError(f"failed to resolve provider '{provider}'")
    provider_data = cast(dict[str, object], provider_data)
    mode_data = provider_data.get(mode)
    if not isinstance(mode_data, dict):
        raise JriError(f"failed to resolve provider '{provider}' mode '{mode}'")
    mode_data = cast(dict[str, object], mode_data)
    return {
        name: _extract_provider_model(mode_data, provider, mode, name)
        for name in _provider_fields(mode)
    }


def _provider_fields(mode: ProviderMode) -> tuple[str, ...]:
    if mode == "chat":
        return tuple(_CHAT_AGENT_NAMES)
    return tuple(_START_AGENT_NAMES)


def _extract_provider_model(
    mode_data: dict[str, object], provider: str, mode: ProviderMode, field_name: str
) -> str:
    value = mode_data.get(field_name)
    if not isinstance(value, str):
        raise JriError(
            "failed to resolve provider model for "
            f"provider '{provider}' mode '{mode}' field '{field_name}'"
        )
    return value


def _load_provider_config() -> dict[str, object]:
    config_text = (
        files("jri.core").joinpath("providers.json").read_text(encoding="utf-8")
    )
    loaded = json.loads(config_text)
    if not isinstance(loaded, dict):
        raise JriError("provider config must be a JSON object")
    return loaded
