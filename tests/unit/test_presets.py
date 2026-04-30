import pytest

from jri.core.agents.presets import preset_choices, resolve_preset_models
from jri.core.errors import JriError


def test_preset_choices_are_stable() -> None:
    assert preset_choices() == ("default", "openai")


def test_resolve_preset_models_default_is_noop() -> None:
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
        "model": None,
        "validator_model": None,
        "general_model": None,
        "explore_model": None,
    }


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
        "model": "openai-codex/gpt-5.4",
        "validator_model": "openai-codex/gpt-5.4",
        "general_model": "openai-codex/gpt-5.3-codex",
        "explore_model": "openai-codex/gpt-5.4-mini",
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
        "model": "openai-codex/gpt-5.4",
        "validator_model": "openai-codex/gpt-5.4",
        "explore_model": "openai-codex/gpt-5.4-mini",
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
        "model": "openai-codex/gpt-5.4",
        "validator_model": "openai/gpt-5.4-mini",
        "general_model": "openai/gpt-5.4",
        "explore_model": "openai-codex/gpt-5.4-mini",
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
