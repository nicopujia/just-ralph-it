from typing import Literal, cast

from ..errors import JriError

PresetMode = Literal["chat", "start"]

_CHAT_FIELDS = ("model", "explore_model")
_START_FIELDS = ("model", "validator_model", "general_model", "explore_model")

_PRESET_CHOICES = ("default", "openai")
_DEFAULT_CHAT_MODEL = "openrouter/z-ai/glm-5.1"
_DEFAULT_START_MODEL = "openrouter/moonshotai/kimi-k2.6"
_DEFAULT_VALIDATOR_MODEL = "openrouter/deepseek/deepseek-r1-0528"
_DEFAULT_GENERAL_MODEL = "openrouter/qwen/qwen3-30b-a3b-thinking-2507"
_DEFAULT_EXPLORE_MODEL = "openrouter/z-ai/glm-4.7-flash"
_PRESET_CONFIG: dict[str, object] = {
    "default": {
        "chat": {
            "model": _DEFAULT_CHAT_MODEL,
            "explore_model": _DEFAULT_EXPLORE_MODEL,
        },
        "start": {
            "model": _DEFAULT_START_MODEL,
            "validator_model": _DEFAULT_VALIDATOR_MODEL,
            "general_model": _DEFAULT_GENERAL_MODEL,
            "explore_model": _DEFAULT_EXPLORE_MODEL,
        },
    },
    "openai": {
        "chat": {
            "model": "openai-codex/gpt-5.4",
            "explore_model": "openai-codex/gpt-5.4-mini",
        },
        "start": {
            "model": "openai-codex/gpt-5.4",
            "validator_model": "openai-codex/gpt-5.4",
            "general_model": "openai-codex/gpt-5.3-codex",
            "explore_model": "openai-codex/gpt-5.4-mini",
        },
    },
}


def preset_choices() -> tuple[str, ...]:
    return _PRESET_CHOICES


def resolve_preset_models(
    preset: str | None,
    *,
    mode: PresetMode,
    overrides: dict[str, str | None],
) -> dict[str, str | None]:
    resolved: dict[str, str | None] = {
        name: value for name, value in _preset_models(preset, mode=mode).items()
    }
    for name, value in overrides.items():
        if value is not None:
            resolved[name] = value
        elif name not in resolved:
            resolved[name] = None
    return resolved


def _preset_models(preset: str | None, *, mode: PresetMode) -> dict[str, str]:
    if preset is None:
        return {}
    if preset in _PRESET_CHOICES:
        return _configured_preset_models(preset, mode=mode)
    raise JriError(f"unknown preset '{preset}'")


def _configured_preset_models(preset: str, *, mode: PresetMode) -> dict[str, str]:
    preset_config = _load_preset_config()
    preset_data = preset_config.get(preset)
    if not isinstance(preset_data, dict):
        raise JriError(f"failed to resolve preset '{preset}'")
    preset_data = cast(dict[str, object], preset_data)
    mode_data = preset_data.get(mode)
    if not isinstance(mode_data, dict):
        raise JriError(f"failed to resolve preset '{preset}' mode '{mode}'")
    mode_data = cast(dict[str, object], mode_data)
    return {
        name: _extract_preset_model(mode_data, preset, mode, name)
        for name in _preset_fields(mode)
    }


def _preset_fields(mode: PresetMode) -> tuple[str, ...]:
    if mode == "chat":
        return _CHAT_FIELDS
    return _START_FIELDS


def _extract_preset_model(
    mode_data: dict[str, object], preset: str, mode: PresetMode, field_name: str
) -> str:
    value = mode_data.get(field_name)
    if not isinstance(value, str):
        raise JriError(
            "failed to resolve preset model for "
            f"preset '{preset}' mode '{mode}' field '{field_name}'"
        )
    return value


def _load_preset_config() -> dict[str, object]:
    return _PRESET_CONFIG
