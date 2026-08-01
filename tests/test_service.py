from collections.abc import Iterable
from pathlib import Path
from threading import Event
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
        logging=SimpleNamespace(level="CRITICAL"),
        llm=SimpleNamespace(client=FakeClient(rounds)),
        agents=SimpleNamespace(interviewer=SimpleNamespace(model="test", temperature=0, reasoning_effort=None)),
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


def test_ralph_readiness_survives_restart_and_rolls_back_on_failure(tmp_path: Path) -> None:
    service = build_service(
        tmp_path,
        [
            response(call("ready", "just_ralph_it", show=True)),
            response(reply("Click Just Ralph It.")),
            response(call("hide", "just_ralph_it", show=False)),
            failure("provider failed"),
        ],
    )
    list(service.chat("We're ready."))

    restarted = build_service(tmp_path, [])
    restarted.restore()
    assert restarted.session.ready_to_ralph

    with pytest.raises(RuntimeError, match="provider failed"):
        list(service.chat("Actually, one more thing."))

    assert service.session.ready_to_ralph


def test_cancelled_interview_turn_survives_restart_and_remains_in_context(tmp_path: Path) -> None:
    cancelled = Event()
    service = build_service(
        tmp_path,
        [[SimpleNamespace(type="response.output_text.delta", delta="Partial reply")], response(reply("Next reply"))],
    )
    events = service.chat("Keep this prompt.", cancelled)

    next(events)
    cancelled.set()
    list(events)

    list(service.chat("Continue."))
    context = cast("FakeClient", service.interviewer.client).responses.inputs[-1]
    restarted = build_service(tmp_path, [])
    items, _ = restarted.restore()

    assert {("user", "Keep this prompt."), ("assistant", "Partial reply")} <= {(item.type, item.text) for item in items}
    assert {item["content"] for item in cast("list[dict[str, object]]", context) if "content" in item} >= {
        "Keep this prompt.",
        "Partial reply",
    }


def test_cancelled_interview_turn_without_reply_keeps_prompt(tmp_path: Path) -> None:
    cancelled = Event()
    cancelled.set()
    service = build_service(tmp_path, [[]])

    list(service.chat("Keep this prompt.", cancelled))

    restarted = build_service(tmp_path, [])
    items, _ = restarted.restore()
    assert ("user", "Keep this prompt.") in [(item.type, item.text) for item in items]


def test_cancelling_tool_call_leaves_valid_history(tmp_path: Path) -> None:
    cancelled = Event()
    service = build_service(
        tmp_path, [response(call("switch", "switch_topic", topic="Delivery")), response(reply("Still works."))]
    )
    events = service.chat("Switch topics.", cancelled)

    next(events)
    cancelled.set()
    list(events)
    list(service.chat("Continue."))

    assert service.interviewer.active_topic_id == "t1"
    assert {
        (item.get("type"), item.get("call_id"), item.get("output"))
        for item in cast("list[dict[str, object]]", service.interviewer.history)
    } >= {("function_call_output", "switch", "Tool call cancelled.")}


def test_failed_interview_turn_rolls_back_changes_and_keeps_prompt(tmp_path: Path) -> None:
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

    with pytest.raises(RuntimeError, match="provider failed"):
        list(service.chat("Deploy it automatically."))

    assert service.interviewer.notebook.graph.model_dump() == graph
    assert service.interviewer.history == [*history, {"role": "user", "content": "Deploy it automatically."}]
    assert service.interviewer.active_topic_id == active_topic_id
    assert service.notebook_file.read_bytes() == notebook_file

    restarted = build_service(tmp_path, [])
    items, _ = restarted.restore()
    assert items[-1] == ("user", "Deploy it automatically.", None)


def test_failed_interview_turn_can_be_retried_after_restart(tmp_path: Path) -> None:
    service = build_service(tmp_path, [failure("provider failed")])

    with pytest.raises(RuntimeError, match="provider failed"):
        list(service.chat("Deploy it automatically."))

    restarted = build_service(tmp_path, [response(reply("Retry succeeded."))])
    restarted.restore()
    list(restarted.retry())

    assert [
        item["content"]
        for item in cast("list[dict[str, object]]", restarted.interviewer.history)
        if item.get("role") == "user"
    ].count("Deploy it automatically.") == 1
    assert restarted.interviewer.history[-1]["content"][0]["text"] == "Retry succeeded."


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
            response(
                call("billing-switch", "switch_topic", topic="Billing"),
                call("billing-capture", "capture_notes", texts=["Charge monthly."]),
            ),
            response(reply("Billing captured.")),
        ],
    )
    list(service.chat("Deploy from main."))
    list(service.chat("Encrypt stored credentials."))
    list(service.chat("Charge monthly."))

    restarted = build_service(tmp_path, [])
    restarted.restore()
    restarted.rewind(1)

    reopened = build_service(tmp_path, [])
    items, _ = reopened.restore()
    graph = reopened.interviewer.notebook.graph

    assert reopened.interviewer.active_topic_id == "t2"
    assert {(topic.id, topic.name) for topic in graph.topics} == {("t1", "Project overview"), ("t2", "Delivery")}
    assert [(note.topic_id, note.text) for note in graph.notes] == [("t2", "Deploy from main.")]
    assert {"Encrypt stored credentials.", "Charge monthly."}.isdisjoint(item.text for item in items)


def test_rewind_skips_failed_and_cancelled_tool_calls(tmp_path: Path) -> None:
    cancelled = Event()
    service = build_service(
        tmp_path,
        [
            response(call("failed", "switch_topic", topic="")),
            response(reply("That topic was invalid.")),
            response(call("cancelled", "switch_topic", topic="Delivery")),
            response(reply("Latest turn.")),
        ],
    )
    list(service.chat("Try an invalid topic."))
    events = service.chat("Cancel this switch.", cancelled)
    next(events)
    cancelled.set()
    list(events)
    list(service.chat("Keep this only until rewind."))

    service.rewind(2)

    assert service.interviewer.active_topic_id == "t1"
    assert [(topic.id, topic.name) for topic in service.interviewer.notebook.graph.topics] == [
        ("t1", "Project overview")
    ]
    assert "Keep this only until rewind." not in {
        item["content"]
        for item in cast("list[dict[str, object]]", service.interviewer.history)
        if item.get("role") == "user"
    }


def test_invalid_session_file_explains_how_to_reset(tmp_path: Path) -> None:
    service = build_service(tmp_path, [])
    service.session_file.write_text("not json")

    with pytest.raises(PersistenceError, match="--force"):
        service.restore()


def test_force_resets_invalid_notebook_session(tmp_path: Path) -> None:
    base_dir = tmp_path / ".jri"
    base_dir.mkdir()
    config = base_dir / "config.yaml"
    secrets = base_dir / "secrets.yaml"
    config.write_text("custom config")
    secrets.write_text("custom secrets")
    (base_dir / ".gitignore").write_text("custom-cache\n")
    (base_dir / "notebook.yaml").write_text(": invalid yaml")
    (base_dir / "session.json").write_text("not json")
    (base_dir / "visualization.html").write_text("old graph")
    (base_dir / "logs").mkdir()
    (base_dir / "logs" / "old.log").write_text("old log")
    (base_dir / "specs").mkdir()
    (base_dir / "specs" / "old.md").write_text("old spec")

    service = build_service(tmp_path, [], force=True)

    items, show_thinking_blocks = service.restore()
    assert items == []
    assert show_thinking_blocks is False
    assert [(topic.id, topic.name) for topic in service.interviewer.notebook.graph.topics] == [
        ("t1", "Project overview")
    ]
    assert service.notebook_file == base_dir / "notebook.yaml"
    assert service.visualization_file == base_dir / "visualization.html"
    assert config.read_text() == "custom config"
    assert secrets.read_text() == "custom secrets"
    assert not service.session_file.exists()
    assert not service.visualization_file.exists()
    assert not (base_dir / "specs").exists()
    assert not (base_dir / "logs" / "old.log").exists()
    assert service.gitignore_file.read_text() == "custom-cache\nsecrets.yaml\nsession.json\nlogs\nvisualization.html\n"
