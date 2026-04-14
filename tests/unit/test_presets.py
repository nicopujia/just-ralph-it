import pytest

from jri.core.errors import JriError
from jri.core.opencode.presets import preset_choices, resolve_preset_models


def test_preset_choices_are_stable() -> None:
    assert preset_choices() == ("default", "openai")


def test_resolve_preset_models_uses_openai_start_bundle() -> None:
    resolved = resolve_preset_models(
        "openai",
        mode="start",
        overrides={
            "model": None,
            "validator_model": None,
            "general_model": None,
            "explore_model": None,
        },
    )

    assert resolved == {
        "model": "openai/gpt-5.4-pro",
        "validator_model": "openai/gpt-5.4",
        "general_model": "openai/gpt-5-codex",
        "explore_model": "openai/gpt-5.4-mini",
    }


def test_resolve_preset_models_uses_openai_chat_bundle() -> None:
    resolved = resolve_preset_models(
        "openai",
        mode="chat",
        overrides={
            "model": None,
            "validator_model": None,
            "explore_model": None,
        },
    )

    assert resolved == {
        "model": "openai/gpt-5.4",
        "validator_model": "openai/gpt-5.4",
        "explore_model": "openai/gpt-5.4-mini",
    }


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
        "model": "openai/gpt-5.4-pro",
        "validator_model": "openai/gpt-5.4-mini",
        "general_model": "openai/gpt-5.4",
        "explore_model": "openai/gpt-5.4-mini",
    }


def test_resolve_preset_models_default_matches_checked_in_config() -> None:
    resolved = resolve_preset_models(
        "default",
        mode="start",
        overrides={
            "model": None,
            "validator_model": None,
            "general_model": None,
            "explore_model": None,
        },
    )

    assert resolved == {
        "model": "vercel/moonshotai/kimi-k2.5",
        "validator_model": "vercel/zai/glm-5",
        "general_model": "vercel/deepseek/deepseek-v3.2",
        "explore_model": "vercel/alibaba/qwen3.5-flash",
    }


def test_resolve_preset_models_rejects_unknown_preset() -> None:
    with pytest.raises(JriError, match="unknown preset 'bogus'"):
        resolve_preset_models(
            "bogus",
            mode="chat",
            overrides={
                "model": None,
                "validator_model": None,
                "explore_model": None,
            },
        )
