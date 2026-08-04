from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from jri.core.settings import AgentProfile

if TYPE_CHECKING:
    from jri.core.settings import Settings
    from tests.doubles.openai import FakeClient


def build_settings(
    path: Path, client: "FakeClient", *, temperature: float | None = 0, search_api_key: str | None = None
) -> "Settings":
    profile = AgentProfile(model="test", reasoning_effort=None, temperature=temperature)
    return cast(
        "Settings",
        FakeSettings(
            cwd=path,
            logging=SimpleNamespace(level="CRITICAL"),
            llm=SimpleNamespace(client=client),
            brave_search=SimpleNamespace(api_key=search_api_key),
            agents=SimpleNamespace(
                interviewer=profile, explorer=profile, functional_analyst=profile, architect=profile
            ),
        ),
    )


class FakeSettings(SimpleNamespace):
    def model_copy(self, *, update: dict[str, object]) -> "FakeSettings":
        return FakeSettings(**(vars(self) | update))
