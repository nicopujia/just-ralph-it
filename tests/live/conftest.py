from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def show_live_agent_output(
    run_live_opencode: bool,
    capsys: pytest.CaptureFixture[str],
) -> Iterator[None]:
    if not run_live_opencode:
        yield
        return

    with capsys.disabled():
        yield
