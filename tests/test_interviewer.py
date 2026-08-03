from pathlib import Path
from typing import cast

import pytest

from jri.core.ai import Interviewer
from jri.core.notes import Notebook
from tests.doubles.models import serve_catalog
from tests.doubles.openai import FakeClient
from tests.doubles.settings import build_settings


def build_interviewer(path: Path) -> Interviewer:
    return Interviewer(build_settings(path, FakeClient([])), Notebook(path / "notebook.yaml"))


def test_keeps_at_least_ten_recent_turns_in_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    serve_catalog(monkeypatch, {"test": {"limit": {"context": 1}}})
    interviewer = build_interviewer(tmp_path)
    for index in range(12):
        interviewer.history.extend([
            {"role": "user", "content": f"Question {index}"},
            {"role": "assistant", "content": f"Answer {index}"},
        ])

    context = cast("list[dict[str, object]]", interviewer.get_context())

    messages = [item["content"] for item in context if item.get("role") == "user"]
    assert messages == [f"Question {index}" for index in range(2, 12)]


def test_switching_creates_resolves_and_rejects_topics(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)

    assert interviewer.switch_topic("Delivery") == "Switched to t2: Delivery"
    assert interviewer.switch_topic("t2") == "Switched to t2: Delivery"
    assert interviewer.active_topic_id == "t2"

    interviewer.notebook.add(["Deploy from the main branch."], "t2")

    with pytest.raises(ValueError, match="`n1` is not a topic"):
        interviewer.switch_topic("n1")
    assert interviewer.active_topic_id == "t2"


def test_switching_to_a_trashed_topic_is_rejected(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)
    interviewer.switch_topic("Delivery")
    interviewer.update_topic("t2", "trashed")

    with pytest.raises(ValueError, match="is trashed"):
        interviewer.switch_topic("t2")


def test_trashing_the_active_topic_falls_back_to_the_overview(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)
    interviewer.switch_topic("Delivery")

    assert interviewer.update_topic("t2", "trashed") == "Updated t2: Delivery (trashed)"
    assert interviewer.active_topic_id == "t1"


def test_the_overview_topic_cannot_be_trashed(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)

    with pytest.raises(ValueError, match="cannot be trashed"):
        interviewer.update_topic("t1", "trashed")

    assert interviewer.notebook.graph.topics[0].status == "open"
