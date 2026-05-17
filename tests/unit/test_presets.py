import pytest

from jri.core.agents.presets import preset_choices, resolve_preset_models
from jri.core.errors import JriError

DEFAULT_CHAT_MODEL = "openrouter/z-ai/glm-5.1"
DEFAULT_EXPLORE_MODEL = "openrouter/z-ai/glm-4.7-flash"
DEFAULT_START_MODEL = "openrouter/moonshotai/kimi-k2.6"
DEFAULT_VALIDATOR_MODEL = "openrouter/deepseek/deepseek-r1-0528"
DEFAULT_GENERAL_MODEL = "openrouter/qwen/qwen3-30b-a3b-thinking-2507"


def test_preset_choices_are_stable() -> None:
    assert preset_choices() == ("default", "openai")


def test_resolve_preset_models_uses_default_start_bundle() -> None:
    resolved = resolve_preset_models(
        "default",
        mode="start",
        overrides={"model": None, "validator_model": None, "general_model": None, "explore_model": None},
    )

    assert resolved == {
        "model": DEFAULT_START_MODEL,
        "validator_model": DEFAULT_VALIDATOR_MODEL,
        "general_model": DEFAULT_GENERAL_MODEL,
        "explore_model": DEFAULT_EXPLORE_MODEL,
    }


def test_resolve_preset_models_uses_default_chat_bundle() -> None:
    resolved = resolve_preset_models("default", mode="chat", overrides={"model": None, "explore_model": None})

    assert resolved == {"model": DEFAULT_CHAT_MODEL, "explore_model": DEFAULT_EXPLORE_MODEL}


def test_resolve_preset_models_uses_openai_start_bundle() -> None:
    resolved = resolve_preset_models(
        "openai",
        mode="start",
        overrides={"model": None, "validator_model": None, "general_model": None, "explore_model": None},
    )

    assert resolved == {
        "model": "openai-codex/gpt-5.4",
        "validator_model": "openai-codex/gpt-5.4",
        "general_model": "openai-codex/gpt-5.3-codex",
        "explore_model": "openai-codex/gpt-5.4-mini",
    }


def test_resolve_preset_models_uses_openai_chat_bundle() -> None:
    resolved = resolve_preset_models("openai", mode="chat", overrides={"model": None, "explore_model": None})

    assert resolved == {"model": "openai-codex/gpt-5.4", "explore_model": "openai-codex/gpt-5.4-mini"}


def test_resolve_preset_models_allows_explicit_overrides() -> None:
    resolved = resolve_preset_models(
        "openai",
        mode="start",
        overrides={
            "model": None,
            "validator_model": "openai/gpt-5.4-mini",
            "general_model": "openai/gpt-5.4",
            "explore_model": None,
        },
    )

    assert resolved == {
        "model": "openai-codex/gpt-5.4",
        "validator_model": "openai/gpt-5.4-mini",
        "general_model": "openai/gpt-5.4",
        "explore_model": "openai-codex/gpt-5.4-mini",
    }


def test_resolve_preset_models_rejects_unknown_preset() -> None:
    with pytest.raises(JriError, match="unknown preset 'bogus'"):
        resolve_preset_models(
            "bogus", mode="chat", overrides={"model": None, "validator_model": None, "explore_model": None}
        )
