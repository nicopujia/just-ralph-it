"""Provider registry discovery."""

from importlib import import_module
from pkgutil import iter_modules
from typing import TYPE_CHECKING, cast

from jri.core.agents.models import ProviderModelRegistry

if TYPE_CHECKING:
    from collections.abc import Iterable

PROVIDER_PACKAGE = "jri.core.agents.providers"


def load_provider_registries() -> dict[str, ProviderModelRegistry]:
    """Return provider registries discovered from provider modules."""
    registries: dict[str, ProviderModelRegistry] = {}
    provider_package = import_module(PROVIDER_PACKAGE)
    provider_paths = cast("list[str]", vars(provider_package)["__path__"])
    for module_info in iter_modules(
        provider_paths,
        f"{provider_package.__name__}.",
    ):
        module = import_module(module_info.name)
        for value in cast("Iterable[object]", vars(module).values()):
            if isinstance(value, ProviderModelRegistry):
                registries[value.provider] = value
    return registries
