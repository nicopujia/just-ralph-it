from collections.abc import Iterator
from contextlib import AbstractContextManager
from typing import Protocol

import pytest


class _CaptureManager(Protocol):
    def global_and_fixture_disabled(
        self,
    ) -> AbstractContextManager[object, bool | None]: ...


@pytest.fixture(autouse=True)
def show_live_agent_output(
    run_live_opencode: bool,
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    if not run_live_opencode:
        yield
        return

    capturemanager = request.config.pluginmanager.getplugin("capturemanager")
    if capturemanager is None:
        yield
        return

    with capturemanager.global_and_fixture_disabled():
        yield
