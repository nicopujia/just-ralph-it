"""Live smoke tests for OpenRouter model presets."""

from collections.abc import Mapping
from typing import cast

import httpx
import pytest

from jri.core.agents.providers.openrouter import OPENROUTER_REGISTRY

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


def test_live_openrouter_default_models_are_available(live: bool) -> None:
    """Default OpenRouter models exist and support tool calls."""
    if not live:
        pytest.skip("use --live to run real OpenRouter model smoke tests")

    response = httpx.get(OPENROUTER_MODELS_URL, timeout=10.0)
    response.raise_for_status()
    catalog = _model_catalog(cast("object", response.json()))
    config = OPENROUTER_REGISTRY.load_model_config(
        preset_name="cheap",
        env={},
    )

    for model_id in {
        config.interviewer.removeprefix("openrouter:"),
        config.explorer.removeprefix("openrouter:"),
    }:
        assert model_id in catalog
        supported = catalog[model_id].get("supported_parameters")
        assert isinstance(supported, list)
        assert "tools" in cast("list[object]", supported)


def _model_catalog(payload: object) -> dict[str, Mapping[str, object]]:
    assert isinstance(payload, Mapping)
    payload_mapping = cast("Mapping[str, object]", payload)
    data = payload_mapping.get("data")
    assert isinstance(data, list)
    catalog: dict[str, Mapping[str, object]] = {}
    for raw_item in cast("list[object]", data):
        if not isinstance(raw_item, Mapping):
            continue
        item = cast("Mapping[str, object]", raw_item)
        model_id = item.get("id")
        if isinstance(model_id, str):
            catalog[model_id] = item
    return catalog
