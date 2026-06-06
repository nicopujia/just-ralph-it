"""Tests for provider registry discovery."""

from jri.core.agents.providers.openrouter import OPENROUTER_REGISTRY
from jri.core.agents.providers.registry import load_provider_registries


def test_load_provider_registries_discovers_provider_modules() -> None:
    """Provider registries are discovered from provider modules."""
    registries = load_provider_registries()

    assert registries == {"openrouter": OPENROUTER_REGISTRY}
