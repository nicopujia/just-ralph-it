from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from jri.core.exceptions import PersistenceError
from jri.core.service import Service
from tests.doubles.openai import FakeClient, Round, call, failure, reply, response

if TYPE_CHECKING:
    from jri.core.settings import Settings


def build_service(path: Path, rounds: Iterable[Round], *, force: bool = False) -> Service:
    settings = SimpleNamespace(
        cwd=path,
        force=force,
        logging_level="CRITICAL",
        llm_client=FakeClient(rounds),
        interviewer_model="test",
        interviewer_temperature=0,
        interviewer_reasoning_effort=None,
    )
    return Service(cast("Settings", settings))


def test_completed_interview_turn_survives_restart(tmp_path: Path) -> None:
    service = build_service(
        tmp_path,
        [
            response(
                call("switch", "switch_topic", topic="Delivery"),
                call("capture", "capture_notes", texts=["Deploy from the main branch."]),
            ),
            response(reply("How should failed deployments be handled?")),
        ],
    )

    list(service.chat("Deploy the project automatically."))

    restarted = build_service(tmp_path, [])
    items, _ = restarted.restore()

    assert restarted.interviewer.active_topic_id == "t2"
    assert {(topic.id, topic.name) for topic in restarted.interviewer.notebook.graph.topics} == {
        ("t1", "Project overview"),
        ("t2", "Delivery"),
    }
    assert [(note.topic_id, note.text) for note in restarted.interviewer.notebook.graph.notes] == [
        ("t2", "Deploy from the main branch.")
    ]
    assert ("user", "Deploy the project automatically.") in [(item.type, item.text) for item in items]
    assert ("assistant", "How should failed deployments be handled?") in [(item.type, item.text) for item in items]


def test_failed_interview_turn_rolls_back_every_persisted_change(tmp_path: Path) -> None:
    service = build_service(
        tmp_path,
        [
            response(call("first-capture", "capture_notes", texts=["The project has a terminal UI."])),
            response(reply("What should it display?")),
            response(
                call("switch", "switch_topic", topic="Delivery"),
                call("second-capture", "capture_notes", texts=["Deploy automatically."]),
            ),
            failure("provider failed"),
        ],
    )
    list(service.chat("It has a terminal UI."))
    graph = service.interviewer.notebook.graph.model_dump()
    history = list(service.interviewer.history)
    active_topic_id = service.interviewer.active_topic_id
    notebook_file = service.notebook_file.read_bytes()
    session_file = service.session_file.read_bytes()

    with pytest.raises(RuntimeError, match="provider failed"):
        list(service.chat("Deploy it automatically."))

    assert service.interviewer.notebook.graph.model_dump() == graph
    assert service.interviewer.history == history
    assert service.interviewer.active_topic_id == active_topic_id
    assert service.notebook_file.read_bytes() == notebook_file
    assert service.session_file.read_bytes() == session_file


def test_rewind_removes_later_knowledge_after_restart(tmp_path: Path) -> None:
    service = build_service(
        tmp_path,
        [
            response(
                call("delivery-switch", "switch_topic", topic="Delivery"),
                call("delivery-capture", "capture_notes", texts=["Deploy from main."]),
            ),
            response(reply("Delivery captured.")),
            response(
                call("security-switch", "switch_topic", topic="Security"),
                call("security-capture", "capture_notes", texts=["Encrypt stored credentials."]),
            ),
            response(reply("Security captured.")),
        ],
    )
    list(service.chat("Deploy from main."))
    list(service.chat("Encrypt stored credentials."))

    service.rewind(1)

    restarted = build_service(tmp_path, [])
    items, _ = restarted.restore()
    graph = restarted.interviewer.notebook.graph

    assert restarted.interviewer.active_topic_id == "t2"
    assert {(topic.id, topic.name) for topic in graph.topics} == {("t1", "Project overview"), ("t2", "Delivery")}
    assert [(note.topic_id, note.text) for note in graph.notes] == [("t2", "Deploy from main.")]
    assert "Encrypt stored credentials." not in {item.text for item in items}


def test_invalid_session_file_explains_how_to_reset(tmp_path: Path) -> None:
    service = build_service(tmp_path, [])
    service.session_file.write_text("not json")

    with pytest.raises(PersistenceError, match="--force"):
        service.restore()


def test_force_resets_invalid_notebook_session(tmp_path: Path) -> None:
    base_dir = tmp_path / ".jri"
    base_dir.mkdir()
    (base_dir / "notebook.yaml").write_text(": invalid yaml")
    (base_dir / "session.json").write_text("not json")

    service = build_service(tmp_path, [], force=True)

    items, show_thinking_blocks = service.restore()
    assert items == []
    assert show_thinking_blocks is False
    assert [(topic.id, topic.name) for topic in service.interviewer.notebook.graph.topics] == [
        ("t1", "Project overview")
    ]
    assert service.notebook_file == base_dir / "notebook.yaml"
    assert service.visualization_file == base_dir / "visualization.html"
    assert service.gitignore_file.read_text() == "session.json\nlogs\nvisualization.html\n"
