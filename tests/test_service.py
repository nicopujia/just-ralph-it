import json
from collections.abc import Iterator
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast

import pytest
import yaml

from jri.core import paths
from jri.core.ai import functional_analyst
from jri.core.exceptions import PersistenceError
from jri.core.service import InterviewItem, Service
from jri.core.settings import Settings
from jri.lib import git
from tests.conftest import CreateRepository, RunGit
from tests.doubles.openai import FakeClient, call, failure, partial_reply, reply, response
from tests.doubles.settings import build_settings


def build_service(path: Path, client: FakeClient) -> Service:
    return Service(build_settings(path, client))


def test_initializes_a_workspace_ready_to_use(tmp_path: Path) -> None:
    workspace = Service.init(tmp_path)

    assert workspace == (tmp_path / paths.WORKSPACE_DIR, tmp_path / paths.CONFIG_FILE, True, True)
    assert (tmp_path / paths.CONFIG_FILE).read_text() == Settings.render_config()
    assert (tmp_path / paths.GITIGNORE_FILE).read_text() == "session.json\nlogs\nvisualization.html\n"
    assert yaml.safe_load((tmp_path / paths.NOTEBOOK_FILE).read_text()) == {
        "topics": [{"id": "t1", "name": "Project overview", "status": "open", "notes": {}}],
        "connections": [],
    }
    assert list((tmp_path / paths.LOGS_DIR).iterdir()) == []


def test_commits_the_project_when_it_creates_the_repository(tmp_path: Path, run_git: RunGit) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n")
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / ".DS_Store").write_bytes(b"\x00")

    Service.init(tmp_path)

    repository = git.Repository(tmp_path)
    assert run_git(tmp_path, "show", "-s", "--format=%B") == (
        "jri: initialize project\n\nCo-authored-by: ralphpujia <ralph@pujia.ar>"
    )
    assert run_git(tmp_path, "show", "-s", "--format=%(trailers:key=Co-authored-by,valueonly)") == (
        "ralphpujia <ralph@pujia.ar>"
    )
    assert (tmp_path / paths.PROJECT_GITIGNORE_FILE).read_text() == ".DS_Store\n.env\n.env.*\n"
    assert set(repository.read_tracked_paths()) == {
        paths.PROJECT_GITIGNORE_FILE,
        paths.GITIGNORE_FILE,
        paths.CONFIG_FILE,
        paths.NOTEBOOK_FILE,
        "main.py",
    }
    assert repository.read_status() == ()


def test_keeps_an_existing_ignore_file_when_creating_the_repository(tmp_path: Path) -> None:
    (tmp_path / paths.PROJECT_GITIGNORE_FILE).write_text("build/\n")

    Service.init(tmp_path)

    assert (tmp_path / paths.PROJECT_GITIGNORE_FILE).read_text() == "build/\n"


def test_initializes_a_workspace_inside_an_existing_repository(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path / "repo")
    nested = repository.path / "packages" / "app"
    nested.mkdir(parents=True)

    workspace = Service.init(nested)

    assert not workspace.repository_created
    assert repository.read_head() == git.Repository(nested).read_head()
    assert (nested / paths.CONFIG_FILE).exists()


def test_preserves_an_existing_workspace_when_initializing_again(tmp_path: Path) -> None:
    (tmp_path / paths.WORKSPACE_DIR).mkdir()
    (tmp_path / paths.CONFIG_FILE).write_text("custom config\n")
    (tmp_path / paths.GITIGNORE_FILE).write_text("custom-cache\nlogs")

    Service.init(tmp_path)
    workspace = Service.init(tmp_path)

    assert not workspace.created
    assert (tmp_path / paths.CONFIG_FILE).read_text() == "custom config\n"
    assert (tmp_path / paths.GITIGNORE_FILE).read_text() == "custom-cache\nlogs\nsession.json\nvisualization.html\n"


def test_starts_the_workspace_over_when_initialization_is_forced(tmp_path: Path) -> None:
    notebook = {
        "topics": [{"id": "t1", "name": "Project overview", "status": "open", "notes": {"n1": "Keep this note."}}],
        "connections": [],
    }
    Service.init(tmp_path)
    (tmp_path / paths.CONFIG_FILE).write_text("custom config\n")
    (tmp_path / paths.NOTEBOOK_FILE).write_text(yaml.safe_dump(notebook))

    workspace = Service.init(tmp_path, force=True)

    assert not workspace.created
    assert (tmp_path / paths.CONFIG_FILE).read_text() == Settings.render_config()
    assert yaml.safe_load((tmp_path / paths.NOTEBOOK_FILE).read_text()) == {
        "topics": [{"id": "t1", "name": "Project overview", "status": "open", "notes": {}}],
        "connections": [],
    }


def test_reads_the_notes_without_reaching_the_provider(tmp_path: Path) -> None:
    unreachable = build_settings(tmp_path, FakeClient([])).model_copy(update={"llm": SimpleNamespace()})

    service = Service(unreachable)

    assert [topic.id for topic in service.notebook.graph.topics] == ["t1"]


def test_restores_a_completed_interview_turn(tmp_path: Path) -> None:
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


def test_restores_ralph_readiness_after_restart(tmp_path: Path) -> None:
    service = build_service(
        tmp_path,
        FakeClient([response(call("ready", "just_ralph_it", show=True)), response(reply("Click Just Ralph It."))]),
    )
    list(service.chat("We're ready."))

    restarted = build_service(tmp_path, FakeClient([]))
    restarted.restore()

    assert restarted.session.ready_to_ralph


def test_rolls_back_ralph_readiness_when_the_turn_fails(tmp_path: Path) -> None:
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

    with pytest.raises(RuntimeError, match="provider failed"):
        list(service.chat("Actually, one more thing."))

    assert service.session.ready_to_ralph


def test_restores_ralph_readiness_after_an_interrupted_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_asks_the_interviewer_about_the_ambiguities_ralph_found(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    create_repository(tmp_path)
    Service.init(tmp_path)
    ambiguity = "Choose whether output is JSON or plain text."
    client = FakeClient(
        [response(reply("Understood.")), response(reply("Should the output be JSON or plain text?"))],
        parsed=[
            functional_analyst.Output(
                result=functional_analyst.Ambiguities(outcome="ambiguities", ambiguities=[ambiguity])
            )
        ],
    )
    service = build_service(tmp_path, client)
    list(service.chat("Build a reporting CLI."))

    list(service.ralph())

    assert any(ambiguity in item.get("content", "") for item in service.session.interview)
    restarted = build_service(tmp_path, FakeClient([]))
    turns, _ = restarted.restore()
    assert ("assistant", "Should the output be JSON or plain text?", None) in turns[-1].items
    assert restarted.session.active_spec_commit is None


def test_restores_a_cancelled_interview_turn(tmp_path: Path) -> None:
    cancelled = Event()
    service = build_service(tmp_path, FakeClient([partial_reply("Partial reply")]))
    events = service.chat("Keep this prompt.", cancelled)

    next(events)
    cancelled.set()
    list(events)

    turns, _ = build_service(tmp_path, FakeClient([])).restore()
    cancelled_turn = next(turn for turn in turns if turn.message == "Keep this prompt.")
    assert ("assistant", "Partial reply") in [(item.type, item.text) for item in cancelled_turn.items]


def test_keeps_a_cancelled_reply_in_the_model_context(tmp_path: Path) -> None:
    cancelled = Event()
    client = FakeClient([partial_reply("Partial reply"), response(reply("Next reply"))])
    service = build_service(tmp_path, client)
    events = service.chat("Keep this prompt.", cancelled)
    next(events)
    cancelled.set()
    list(events)

    list(service.chat("Continue."))

    context = cast("list[dict[str, object]]", client.responses.inputs[-1])
    assert {item["content"] for item in context if "content" in item} >= {"Keep this prompt.", "Partial reply"}


def test_keeps_the_prompt_of_a_cancelled_turn_without_a_reply(tmp_path: Path) -> None:
    cancelled = Event()
    cancelled.set()
    service = build_service(tmp_path, FakeClient([[]]))

    list(service.chat("Keep this prompt.", cancelled))

    restarted = build_service(tmp_path, FakeClient([]))
    turns, _ = restarted.restore()
    assert "Keep this prompt." in [turn.message for turn in turns]


def test_marks_a_cancelled_turn_without_a_reply_as_stopped(tmp_path: Path) -> None:
    cancelled = Event()
    cancelled.set()
    service = build_service(tmp_path, FakeClient([[]]))

    list(service.chat("Stop this one.", cancelled))

    turns, _ = build_service(tmp_path, FakeClient([])).restore()
    assert turns[-1] == ("Stop this one.", [InterviewItem("stopped")])


def test_leaves_a_cancelled_turn_with_a_reply_unmarked(tmp_path: Path) -> None:
    cancelled = Event()
    service = build_service(tmp_path, FakeClient([partial_reply("Partial reply")]))
    events = service.chat("Stop this one.", cancelled)

    next(events)
    cancelled.set()
    list(events)

    turns, _ = build_service(tmp_path, FakeClient([])).restore()
    assert turns[-1] == ("Stop this one.", [InterviewItem("assistant", "Partial reply")])


def test_clears_the_stopped_mark_on_the_next_turn(tmp_path: Path) -> None:
    cancelled = Event()
    cancelled.set()
    service = build_service(tmp_path, FakeClient([[], response(reply("Carrying on."))]))

    list(service.chat("Stop this one.", cancelled))
    list(service.chat("Carry on."))

    restarted = build_service(tmp_path, FakeClient([]))
    turns, _ = restarted.restore()
    assert not restarted.session.stopped_turn
    assert [item.type for turn in turns for item in turn.items] == ["assistant"]


def test_leaves_valid_history_when_a_tool_call_is_cancelled(tmp_path: Path) -> None:
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


def test_rolls_back_the_changes_of_a_failed_turn(tmp_path: Path) -> None:
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


def test_restores_the_prompt_of_a_failed_turn(tmp_path: Path) -> None:
    service = build_service(tmp_path, FakeClient([failure("provider failed")]))

    with pytest.raises(RuntimeError, match="provider failed"):
        list(service.chat("Deploy it automatically."))

    turns, _ = build_service(tmp_path, FakeClient([])).restore()
    assert turns[-1] == ("Deploy it automatically.", [])


def test_retries_a_failed_turn_after_restart(tmp_path: Path) -> None:
    service = build_service(tmp_path, FakeClient([failure("provider failed")]))

    with pytest.raises(RuntimeError, match="provider failed"):
        list(service.chat("Deploy it automatically."))

    restarted = build_service(tmp_path, FakeClient([response(reply("Retry succeeded."))]))
    restarted.restore()
    list(restarted.retry())

    turns, _ = build_service(tmp_path, FakeClient([])).restore()
    assert [turn.message for turn in turns] == ["Deploy it automatically."]
    assert ("assistant", "Retry succeeded.") in [(item.type, item.text) for item in turns[-1].items]


def test_restores_the_error_of_a_failed_turn(tmp_path: Path) -> None:
    service = build_service(tmp_path, FakeClient([failure("provider failed")]))

    with pytest.raises(RuntimeError, match="provider failed"):
        list(service.chat("Deploy it automatically."))
    service.update_session(failed_turn_error="The provider failed.")

    turns, _ = build_service(tmp_path, FakeClient([])).restore()
    assert turns[-1] == ("Deploy it automatically.", [InterviewItem("error", "The provider failed.")])


def test_clears_the_failed_turn_error_on_a_successful_retry(tmp_path: Path) -> None:
    service = build_service(tmp_path, FakeClient([failure("provider failed"), response(reply("Retry succeeded."))]))

    with pytest.raises(RuntimeError, match="provider failed"):
        list(service.chat("Deploy it automatically."))
    service.update_session(failed_turn_error="The provider failed.")
    list(service.retry())

    restarted = build_service(tmp_path, FakeClient([]))
    turns, _ = restarted.restore()
    assert restarted.session.failed_turn_error is None
    assert [item.type for item in turns[-1].items] == ["assistant"]


def test_clears_the_failed_turn_error_on_a_cancelled_retry(tmp_path: Path) -> None:
    cancelled = Event()
    service = build_service(tmp_path, FakeClient([failure("provider failed"), partial_reply("Partial reply")]))

    with pytest.raises(RuntimeError, match="provider failed"):
        list(service.chat("Deploy it automatically."))
    service.update_session(failed_turn_error="The provider failed.")
    events = service.retry(cancelled)
    next(events)
    cancelled.set()
    list(events)

    restarted = build_service(tmp_path, FakeClient([]))
    turns, _ = restarted.restore()
    assert restarted.session.failed_turn_error is None
    assert [item.type for item in turns[-1].items] == ["assistant"]


def test_clears_the_failed_turn_error_when_rewinding(tmp_path: Path) -> None:
    service = build_service(tmp_path, FakeClient([response(reply("What should it display?")), failure("provider")]))

    list(service.chat("It has a terminal UI."))
    with pytest.raises(RuntimeError, match="provider"):
        list(service.chat("Deploy it automatically."))
    service.update_session(failed_turn_error="The provider failed.")
    service.rewind(1)

    restarted = build_service(tmp_path, FakeClient([]))
    turns, _ = restarted.restore()
    assert restarted.session.failed_turn_error is None
    assert [turn.message for turn in turns] == ["It has a terminal UI."]
    assert [item.type for item in turns[-1].items] == ["assistant"]


def test_removes_knowledge_captured_after_the_rewind_point(tmp_path: Path) -> None:
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


def test_skips_failed_and_cancelled_tool_calls_when_rewinding(tmp_path: Path) -> None:
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


def test_skips_cancelled_tool_calls_when_rewinding_after_restart(tmp_path: Path) -> None:
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


def test_stores_the_session_as_compact_json(tmp_path: Path) -> None:
    service = build_service(tmp_path, FakeClient([response(reply("Noted."))]))

    list(service.chat("Deploy the project automatically."))

    content = service.session_file.read_text(encoding="utf-8")
    assert content == json.dumps(json.loads(content), separators=(",", ":"), ensure_ascii=False)


def test_explains_how_to_reset_an_invalid_session_file(tmp_path: Path) -> None:
    service = build_service(tmp_path, FakeClient([]))
    service.session_file.write_text("not json")

    with pytest.raises(PersistenceError, match=r"Delete it .*--force"):
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

    Service.init(tmp_path, force=True)
    service = build_service(tmp_path, FakeClient([]))

    turns, show_thinking_blocks = service.restore()
    assert turns == []
    assert show_thinking_blocks is False
    assert [(topic.id, topic.name) for topic in service.interviewer.notebook.graph.topics] == [
        ("t1", "Project overview")
    ]
    assert service.notebook_file == base_dir / "notebook.yaml"
    assert service.visualization_file == base_dir / "visualization.html"
    assert config.read_text() == Settings.render_config()
    assert not service.session_file.exists()
    assert not service.visualization_file.exists()
    assert not (base_dir / "specs").exists()
    assert not (base_dir / "logs" / "old.log").exists()
    assert (base_dir / ".gitignore").read_text() == "custom-cache\nsession.json\nlogs\nvisualization.html\n"


def test_keeps_the_rest_of_the_project_when_resetting_the_workspace(tmp_path: Path) -> None:
    for name in (paths.ARCHITECTURE_SPECS_ROOT, paths.FUNCTIONAL_SPECS_ROOT, "src"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "file.md").write_text("project content")

    Service.init(tmp_path, force=True)

    kept = {".jri", ".git", paths.PROJECT_GITIGNORE_FILE}
    assert [
        (path.name, (path / "file.md").read_text()) for path in sorted(tmp_path.iterdir()) if path.name not in kept
    ] == [("architecture", "project content"), ("functional", "project content"), ("src", "project content")]
