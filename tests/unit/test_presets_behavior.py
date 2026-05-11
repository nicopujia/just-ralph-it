import pytest

from jri.core.agents import presets
from jri.core.errors import JriError


def test_resolve_preset_models_rejects_missing_preset_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(presets, "_load_preset_config", lambda: {"default": None})

    with pytest.raises(JriError, match="failed to resolve preset 'default'"):
        presets.resolve_preset_models(
            "default",
            mode="chat",
            overrides={"model": None, "explore_model": None},
        )


def test_resolve_preset_models_rejects_missing_preset_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_load_preset_config() -> dict[str, object]:
        return {"default": {}}

    monkeypatch.setattr(presets, "_load_preset_config", fake_load_preset_config)

    with pytest.raises(
        JriError, match="failed to resolve preset 'default' mode 'start'"
    ):
        presets.resolve_preset_models(
            "default",
            mode="start",
            overrides={
                "model": None,
                "validator_model": None,
                "general_model": None,
                "explore_model": None,
            },
        )


def test_resolve_preset_models_rejects_missing_preset_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        presets,
        "_load_preset_config",
        lambda: {"default": {"chat": {"model": "openrouter/z-ai/glm-5.1"}}},
    )

    with pytest.raises(
        JriError,
        match=(
            "failed to resolve preset model for preset 'default' mode 'chat' "
            "field 'explore_model'"
        ),
    ):
        presets.resolve_preset_models(
            "default",
            mode="chat",
            overrides={"model": None, "explore_model": None},
        )
