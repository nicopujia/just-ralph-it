import re
from typing import Literal

from .errors import JriError
from .opencode.config import load_config_text

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
    config_text = load_config_text()
    return {
        name: _extract_provider_model(config_text, provider, mode, name)
        for name in _provider_fields(mode)
    }


def _provider_fields(mode: ProviderMode) -> tuple[str, ...]:
    if mode == "chat":
        return tuple(_CHAT_AGENT_NAMES)
    return tuple(_START_AGENT_NAMES)


def _extract_provider_model(
    config_text: str, provider: str, mode: ProviderMode, field_name: str
) -> str:
    pattern = re.compile(
        rf'"provider"\s*:\s*\{{.*?"{re.escape(provider)}"\s*:\s*\{{.*?'
        rf'"{re.escape(mode)}"\s*:\s*\{{.*?"{re.escape(field_name)}"\s*:\s*"([^"]+)"',
        re.DOTALL,
    )
    match = pattern.search(config_text)
    if match is None:
        raise JriError(
            "failed to resolve provider model for "
            f"provider '{provider}' field '{field_name}'"
        )
    return match.group(1)
