from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from jri.core.settings import Agent

if TYPE_CHECKING:
    from jri.core.settings import Settings
    from tests.doubles.openai import FakeClient


class FakeSettings(SimpleNamespace):
    """Stand-in for the settings an agent or service reads."""

    def model_copy(self, *, update: dict[str, object]) -> "FakeSettings":
        return FakeSettings(**(vars(self) | update))


def build_settings(
    path: Path, client: "FakeClient", *, temperature: float | None = 0, search_api_key: str | None = None
) -> "Settings":
    """Build the settings an agent or service reads.

    Returns:
        Settings backed by a fake LLM client.
    """

    agent = Agent(model="test", reasoning_effort=None, temperature=temperature)
    return cast(
        "Settings",
        FakeSettings(
            cwd=path,
            force=False,
            logging=SimpleNamespace(level="CRITICAL"),
            llm=SimpleNamespace(client=client),
            brave_search=SimpleNamespace(api_key=search_api_key),
            agents=SimpleNamespace(interviewer=agent, explorer=agent, functional_analyst=agent, architect=agent),
        ),
    )
