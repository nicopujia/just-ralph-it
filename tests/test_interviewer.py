from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from jri.core.agents import Interviewer
from jri.core.notes import Notebook
from tests.doubles.openai import FakeClient

if TYPE_CHECKING:
    from pathlib import Path

    from jri.core.settings import Settings


def test_context_keeps_at_least_ten_recent_turns(monkeypatch: pytest.MonkeyPatch, tmp_path: "Path") -> None:
    monkeypatch.setattr("jri.core.agents.interviewer.get_context_limit", lambda _: 1)
    settings = SimpleNamespace(
        llm_client=FakeClient([]),
        interviewer_model="test",
        interviewer_temperature=0,
        interviewer_reasoning_effort=None,
    )
    interviewer = Interviewer(cast("Settings", settings), Notebook(tmp_path / "notebook.yaml"))
    for index in range(12):
        interviewer.history.extend([
            {"role": "user", "content": f"Question {index}"},
            {"role": "assistant", "content": f"Answer {index}"},
        ])

    context = cast("list[dict[str, object]]", interviewer.get_context())

    messages = [item["content"] for item in context if item.get("role") == "user"]
    assert messages == [f"Question {index}" for index in range(2, 12)]
