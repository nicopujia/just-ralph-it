from collections.abc import Iterator
from pathlib import Path
from threading import Event
from typing import cast

import pytest

from jri.core import paths
from jri.core.exceptions import PersistenceError
from jri.core.service import Service
from jri.core.settings import Settings
from tests.doubles.openai import FakeClient, call, failure, partial_reply, reply, response
from tests.doubles.settings import build_settings


def build_service(path: Path, client: FakeClient, *, force: bool = False) -> Service:
    return Service(build_settings(path, client, force=force))


def test_initializes_a_workspace_ready_to_use(tmp_path: Path) -> None:
    Service.init(tmp_path)

    assert (tmp_path / paths.CONFIG_FILE).read_text() == Settings.render_config()
    assert (tmp_path / paths.GITIGNORE_FILE).read_text() == "session.json\nlogs\nvisualization.html\n"


def test_initializing_an_existing_workspace_preserves_it(tmp_path: Path) -> None:
    (tmp_path / paths.WORKSPACE_DIR).mkdir()
    (tmp_path / paths.CONFIG_FILE).write_text("custom config\n")
    (tmp_path / paths.GITIGNORE_FILE).write_text("custom-cache\nlogs")

    Service.init(tmp_path)
    Service.init(tmp_path)

    assert (tmp_path / paths.CONFIG_FILE).read_text() == "custom config\n"
    assert (tmp_path / paths.GITIGNORE_FILE).read_text() == "custom-cache\nlogs\nsession.json\nvisualization.html\n"


def test_completed_interview_turn_survives_restart(tmp_path: Path) -> None:
    service = build_service(
        tmp_path,
        FakeClient([
            response(
                call("switch", "switch_topic", topic="Delivery"),
                call("capture", "capture_notes", texts=["Deploy from the main branch."]),
            ),
            response(reply("How should failed deployments be handled?")),
        ]),
    )

    list(service.chat("Deploy the project automatically."))

    restarted = build_service(tmp_path, FakeClient([]))
    turns, _ = restarted.restore()

    assert restarted.interviewer.active_topic_id == "t2"
    assert {(topic.id, topic.name) for topic in restarted.interviewer.notebook.graph.topics} == {
        ("t1", "Project overview"),
        ("t2", "Delivery"),
    }
    assert [(note.topic_id, note.text) for note in restarted.interviewer.notebook.graph.notes] == [
        ("t2", "Deploy from the main branch.")
    ]
    assert "Deploy the project automatically." in [turn.message for turn in turns]
    assert ("assistant", "How should failed deployments be handled?") in [
        (item.type, item.text) for turn in turns for item in turn.items
    ]


def test_groups_every_restored_item_under_the_prompt_that_caused_it(tmp_path: Path) -> None:
    service = build_service(
        tmp_path,
        FakeClient([
            response(call("switch", "switch_topic", topic="Delivery")),
            response(reply("Noted.")),
            response(call("capture", "capture_notes", texts=["Deploy from the main branch."])),
            response(reply("Anything else?")),
        ]),
    )
    list(service.chat("First prompt."))
    list(service.chat("Second prompt."))

    turns, _ = build_service(tmp_path, FakeClient([])).restore()

    assert [turn.message for turn in turns] == ["First prompt.", "Second prompt."]
    assert [item.type for item in turns[0].items] == ["tool", "assistant"]
    assert [item.text for item in turns[0].items] == ["Switched to Delivery", "Noted."]
    assert [item.type for item in turns[1].items] == ["tool", "assistant"]
    assert turns[1].items[-1].text == "Anything else?"


def test_ralph_readiness_survives_restart_and_rolls_back_on_failure(tmp_path: Path) -> None:
    service = build_service(
        tmp_path,
        FakeClient([
            response(call("ready", "just_ralph_it", show=True)),
            response(reply("Click Just Ralph It.")),
            response(call("hide", "just_ralph_it", show=False)),
            failure("provider failed"),
        ]),
    )
    list(service.chat("We're ready."))

    restarted = build_service(tmp_path, FakeClient([]))
    restarted.restore()
    assert restarted.session.ready_to_ralph

    with pytest.raises(RuntimeError, match="provider failed"):
        list(service.chat("Actually, one more thing."))

    assert service.session.ready_to_ralph


def test_interrupted_ralph_restores_readiness_after_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class InterruptibleSpecsGen:
        def __init__(self, _settings: "Settings") -> None:
            pass

        @staticmethod
        def generate(_active_commit: str | None) -> Iterator[object]:
            yield object()

    service = build_service(
        tmp_path,
        FakeClient([response(call("ready", "just_ralph_it", show=True)), response(reply("Click Just Ralph It."))]),
    )
    list(service.chat("We're ready."))
    monkeypatch.setattr("jri.core.service.SpecsGen", InterruptibleSpecsGen)

    events = service.ralph()
    next(events)
    assert not service.session.ready_to_ralph
    events.close()

    restarted = build_service(tmp_path, FakeClient([]))
    restarted.restore()
    assert restarted.session.ready_to_ralph


def test_cancelled_interview_turn_survives_restart_and_remains_in_context(tmp_path: Path) -> None:
    cancelled = Event()
    client = FakeClient([partial_reply("Partial reply"), response(reply("Next reply"))])
    service = build_service(tmp_path, client)
    events = service.chat("Keep this prompt.", cancelled)

    next(events)
    cancelled.set()
    list(events)

    list(service.chat("Continue."))
    context = cast("list[dict[str, object]]", client.responses.inputs[-1])
    restarted = build_service(tmp_path, FakeClient([]))
    turns, _ = restarted.restore()

    cancelled_turn = next(turn for turn in turns if turn.message == "Keep this prompt.")
    assert ("assistant", "Partial reply") in [(item.type, item.text) for item in cancelled_turn.items]
    assert {item["content"] for item in context if "content" in item} >= {"Keep this prompt.", "Partial reply"}


def test_cancelled_interview_turn_without_reply_keeps_prompt(tmp_path: Path) -> None:
    cancelled = Event()
    cancelled.set()
    service = build_service(tmp_path, FakeClient([[]]))

    list(service.chat("Keep this prompt.", cancelled))

    restarted = build_service(tmp_path, FakeClient([]))
    turns, _ = restarted.restore()
    assert "Keep this prompt." in [turn.message for turn in turns]


def test_cancelling_a_tool_call_leaves_valid_history(tmp_path: Path) -> None:
    cancelled = Event()
    client = FakeClient([response(call("switch", "switch_topic", topic="Delivery")), response(reply("Still works."))])
    service = build_service(tmp_path, client)
    events = service.chat("Switch topics.", cancelled)

    next(events)
    cancelled.set()
    list(events)
    list(service.chat("Continue."))

    context = cast("list[dict[str, object]]", client.responses.inputs[-1])
    assert service.interviewer.active_topic_id == "t1"
    assert {item["call_id"] for item in context if item.get("type") == "function_call"} == {"switch"}
    assert {item["call_id"] for item in context if item.get("type") == "function_call_output"} == {"switch"}


def test_failed_interview_turn_rolls_back_changes_and_keeps_prompt(tmp_path: Path) -> None:
    service = build_service(
        tmp_path,
        FakeClient([
            response(call("first-capture", "capture_notes", texts=["The project has a terminal UI."])),
            response(reply("What should it display?")),
            response(
                call("switch", "switch_topic", topic="Delivery"),
                call("second-capture", "capture_notes", texts=["Deploy automatically."]),
            ),
            failure("provider failed"),
        ]),
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

    restarted = build_service(tmp_path, FakeClient([]))
    turns, _ = restarted.restore()
    assert turns[-1] == ("Deploy it automatically.", [])


def test_failed_interview_turn_can_be_retried_after_restart(tmp_path: Path) -> None:
    service = build_service(tmp_path, FakeClient([failure("provider failed")]))

    with pytest.raises(RuntimeError, match="provider failed"):
        list(service.chat("Deploy it automatically."))

    restarted = build_service(tmp_path, FakeClient([response(reply("Retry succeeded."))]))
    restarted.restore()
    list(restarted.retry())

    turns, _ = build_service(tmp_path, FakeClient([])).restore()
    assert [turn.message for turn in turns] == ["Deploy it automatically."]
    assert ("assistant", "Retry succeeded.") in [(item.type, item.text) for item in turns[-1].items]


def test_rewinding_removes_later_knowledge_after_restart(tmp_path: Path) -> None:
    service = build_service(
        tmp_path,
        FakeClient([
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
        ]),
    )
    list(service.chat("Deploy from main."))
    list(service.chat("Encrypt stored credentials."))
    list(service.chat("Charge monthly."))

    restarted = build_service(tmp_path, FakeClient([]))
    restarted.restore()
    restarted.rewind(1)

    reopened = build_service(tmp_path, FakeClient([]))
    turns, _ = reopened.restore()
    graph = reopened.interviewer.notebook.graph

    assert reopened.interviewer.active_topic_id == "t2"
    assert {(topic.id, topic.name) for topic in graph.topics} == {("t1", "Project overview"), ("t2", "Delivery")}
    assert [(note.topic_id, note.text) for note in graph.notes] == [("t2", "Deploy from main.")]
    assert {"Encrypt stored credentials.", "Charge monthly."}.isdisjoint(turn.message for turn in turns)


def test_rewinding_skips_failed_and_cancelled_tool_calls(tmp_path: Path) -> None:
    cancelled = Event()
    service = build_service(
        tmp_path,
        FakeClient([
            response(call("failed", "switch_topic", topic="")),
            response(reply("That topic was invalid.")),
            response(call("cancelled", "switch_topic", topic="Delivery")),
            response(reply("Latest turn.")),
        ]),
    )
    list(service.chat("Try an invalid topic."))
    events = service.chat("Cancel this switch.", cancelled)
    next(events)
    cancelled.set()
    list(events)
    list(service.chat("Keep this only until rewind."))

    service.rewind(2)

    turns, _ = build_service(tmp_path, FakeClient([])).restore()
    assert service.interviewer.active_topic_id == "t1"
    assert [(topic.id, topic.name) for topic in service.interviewer.notebook.graph.topics] == [
        ("t1", "Project overview")
    ]
    assert "Keep this only until rewind." not in [turn.message for turn in turns]


def test_rewinding_after_restart_still_skips_cancelled_tool_calls(tmp_path: Path) -> None:
    cancelled = Event()
    service = build_service(tmp_path, FakeClient([response(call("cancelled", "switch_topic", topic="Delivery"))]))
    events = service.chat("Cancel this switch.", cancelled)
    next(events)
    cancelled.set()
    list(events)

    restarted = build_service(tmp_path, FakeClient([response(reply("Latest turn."))]))
    restarted.restore()
    list(restarted.chat("Keep this only until rewind."))
    restarted.rewind(1)

    assert restarted.interviewer.active_topic_id == "t1"
    assert [(topic.id, topic.name) for topic in restarted.interviewer.notebook.graph.topics] == [
        ("t1", "Project overview")
    ]


def test_explains_how_to_reset_an_invalid_session_file(tmp_path: Path) -> None:
    service = build_service(tmp_path, FakeClient([]))
    service.session_file.write_text("not json")

    with pytest.raises(PersistenceError, match="--force"):
        service.restore()


def test_resets_an_invalid_workspace_when_forced(tmp_path: Path) -> None:
    base_dir = tmp_path / ".jri"
    base_dir.mkdir()
    config = base_dir / "config.yaml"
    config.write_text("custom config")
    (base_dir / ".gitignore").write_text("custom-cache\n")
    (base_dir / "notebook.yaml").write_text(": invalid yaml")
    (base_dir / "session.json").write_text("not json")
    (base_dir / "visualization.html").write_text("old graph")
    (base_dir / "logs").mkdir()
    (base_dir / "logs" / "old.log").write_text("old log")
    (base_dir / "specs").mkdir()
    (base_dir / "specs" / "old.md").write_text("old spec")

    service = build_service(tmp_path, FakeClient([]), force=True)

    turns, show_thinking_blocks = service.restore()
    assert turns == []
    assert show_thinking_blocks is False
    assert [(topic.id, topic.name) for topic in service.interviewer.notebook.graph.topics] == [
        ("t1", "Project overview")
    ]
    assert service.notebook_file == base_dir / "notebook.yaml"
    assert service.visualization_file == base_dir / "visualization.html"
    assert config.read_text() == "custom config"
    assert not service.session_file.exists()
    assert not service.visualization_file.exists()
    assert not (base_dir / "specs").exists()
    assert not (base_dir / "logs" / "old.log").exists()
    assert (base_dir / ".gitignore").read_text() == "custom-cache\n"
