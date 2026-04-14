import pytest

from jri.core.errors import JriError
from jri.core.providers import provider_choices, resolve_provider_models


def test_provider_choices_are_stable() -> None:
    assert provider_choices() == ("default", "openai")


def test_resolve_provider_models_uses_openai_start_bundle() -> None:
    resolved = resolve_provider_models(
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


def test_resolve_provider_models_uses_openai_chat_bundle() -> None:
    resolved = resolve_provider_models(
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


def test_resolve_provider_models_allows_explicit_overrides() -> None:
    resolved = resolve_provider_models(
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


def test_resolve_provider_models_default_matches_checked_in_config() -> None:
    resolved = resolve_provider_models(
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


def test_resolve_provider_models_rejects_unknown_provider() -> None:
    with pytest.raises(JriError, match="unknown provider 'bogus'"):
        resolve_provider_models(
            "bogus",
            mode="chat",
            overrides={
                "model": None,
                "validator_model": None,
                "explore_model": None,
            },
        )
