import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast

import pytest

from jri.core import paths
from jri.core.ai import functional_analyst
from jri.core.conversation import Conversation, InterviewItem
from jri.core.exceptions import PersistenceError
from jri.core.workspace import Workspace
from tests.conftest import CreateRepository
from tests.doubles.openai import FakeClient, call, failure, partial_reply, reply, response
from tests.doubles.settings import build_settings
from tests.doubles.specs_gen import InterruptibleSpecsGen


def build_conversation(path: Path, client: FakeClient) -> Conversation:
    return Conversation(build_settings(path, client))


def test_leaves_the_workspace_untouched_until_a_command_reads_it(tmp_path: Path) -> None:
    Workspace.create(tmp_path)
    (tmp_path / paths.NOTEBOOK_FILE).unlink()

    Conversation(build_settings(tmp_path, FakeClient([])))

    assert not (tmp_path / paths.NOTEBOOK_FILE).exists()
    assert list((tmp_path / paths.LOGS_DIR).iterdir()) == []


def test_reads_the_notes_without_reaching_the_provider(tmp_path: Path) -> None:
    unreachable = build_settings(tmp_path, FakeClient([])).model_copy(update={"llm": SimpleNamespace()})

    conversation = Conversation(unreachable)

    assert [topic.id for topic in conversation.notebook.graph.topics] == ["t1"]


def test_restores_a_completed_interview_turn(tmp_path: Path) -> None:
    conversation = build_conversation(
        tmp_path,
        FakeClient([
            response(
                call("switch", "switch_topic", topic="Delivery"),
                call("capture", "capture_notes", texts=["Deploy from the main branch."]),
            ),
            response(reply("How should failed deployments be handled?")),
        ]),
    )

    list(conversation.chat("Deploy the project automatically."))

    restarted = build_conversation(tmp_path, FakeClient([]))
    turns = restarted.restore()

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
    conversation = build_conversation(
        tmp_path,
        FakeClient([
            response(call("switch", "switch_topic", topic="Delivery")),
            response(reply("Noted.")),
            response(call("capture", "capture_notes", texts=["Deploy from the main branch."])),
            response(reply("Anything else?")),
        ]),
    )
    list(conversation.chat("First prompt."))
    list(conversation.chat("Second prompt."))

    turns = build_conversation(tmp_path, FakeClient([])).restore()

    assert [turn.message for turn in turns] == ["First prompt.", "Second prompt."]
    assert [item.type for item in turns[0].items] == ["tool", "assistant"]
    assert [item.text for item in turns[0].items] == ["Switched to Delivery", "Noted."]
    assert [item.type for item in turns[1].items] == ["tool", "assistant"]
    assert turns[1].items[-1].text == "Anything else?"


def test_restores_ralph_readiness_after_restart(tmp_path: Path) -> None:
    conversation = build_conversation(
        tmp_path,
        FakeClient([response(call("ready", "just_ralph_it", show=True)), response(reply("Click Just Ralph It."))]),
    )
    list(conversation.chat("We're ready."))

    restarted = build_conversation(tmp_path, FakeClient([]))
    restarted.restore()

    assert restarted.session.ready_to_ralph


def test_restores_the_thinking_blocks_preference_after_restart(tmp_path: Path) -> None:
    conversation = build_conversation(tmp_path, FakeClient([]))
    conversation.update_session(show_thinking_blocks=True)

    restarted = build_conversation(tmp_path, FakeClient([]))
    restarted.restore()

    assert restarted.session.show_thinking_blocks is True


def test_rolls_back_ralph_readiness_when_the_turn_fails(tmp_path: Path) -> None:
    conversation = build_conversation(
        tmp_path,
        FakeClient([
            response(call("ready", "just_ralph_it", show=True)),
            response(reply("Click Just Ralph It.")),
            response(call("hide", "just_ralph_it", show=False)),
            failure("provider failed"),
        ]),
    )
    list(conversation.chat("We're ready."))

    with pytest.raises(RuntimeError, match="provider failed"):
        list(conversation.chat("Actually, one more thing."))

    assert conversation.session.ready_to_ralph


def test_restores_ralph_readiness_after_an_interrupted_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conversation = build_conversation(
        tmp_path,
        FakeClient([response(call("ready", "just_ralph_it", show=True)), response(reply("Click Just Ralph It."))]),
    )
    list(conversation.chat("We're ready."))
    monkeypatch.setattr("jri.core.conversation.SpecsGen", InterruptibleSpecsGen)

    events = conversation.ralph()
    next(events)
    assert not conversation.session.ready_to_ralph
    events.close()

    restarted = build_conversation(tmp_path, FakeClient([]))
    restarted.restore()
    assert restarted.session.ready_to_ralph


def test_asks_the_interviewer_about_the_ambiguities_ralph_found(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    create_repository(tmp_path)
    Workspace.create(tmp_path)
    ambiguity = "Choose whether output is JSON or plain text."
    client = FakeClient(
        [response(reply("Understood.")), response(reply("Should the output be JSON or plain text?"))],
        parsed=[
            functional_analyst.Output(
                result=functional_analyst.Ambiguities(outcome="ambiguities", ambiguities=[ambiguity])
            )
        ],
    )
    conversation = build_conversation(tmp_path, client)
    list(conversation.chat("Build a reporting CLI."))

    list(conversation.ralph())

    assert any(ambiguity in item.get("content", "") for item in conversation.session.interview)
    restarted = build_conversation(tmp_path, FakeClient([]))
    turns = restarted.restore()
    assert InterviewItem("assistant", "Should the output be JSON or plain text?") in turns[-1].items
    assert restarted.session.active_spec_commit is None


def test_restores_a_cancelled_interview_turn(tmp_path: Path) -> None:
    cancelled = Event()
    conversation = build_conversation(tmp_path, FakeClient([partial_reply("Partial reply")]))
    events = conversation.chat("Keep this prompt.", cancelled)

    next(events)
    cancelled.set()
    list(events)

    turns = build_conversation(tmp_path, FakeClient([])).restore()
    cancelled_turn = next(turn for turn in turns if turn.message == "Keep this prompt.")
    assert ("assistant", "Partial reply") in [(item.type, item.text) for item in cancelled_turn.items]


def test_keeps_a_cancelled_reply_in_the_model_context(tmp_path: Path) -> None:
    cancelled = Event()
    client = FakeClient([partial_reply("Partial reply"), response(reply("Next reply"))])
    conversation = build_conversation(tmp_path, client)
    events = conversation.chat("Keep this prompt.", cancelled)
    next(events)
    cancelled.set()
    list(events)

    list(conversation.chat("Continue."))

    context = cast("list[dict[str, object]]", client.responses.inputs[-1])
    assert {item["content"] for item in context if "content" in item} >= {"Keep this prompt.", "Partial reply"}


def test_keeps_the_prompt_of_a_cancelled_turn_without_a_reply(tmp_path: Path) -> None:
    cancelled = Event()
    cancelled.set()
    conversation = build_conversation(tmp_path, FakeClient([[]]))

    list(conversation.chat("Keep this prompt.", cancelled))

    restarted = build_conversation(tmp_path, FakeClient([]))
    turns = restarted.restore()
    assert "Keep this prompt." in [turn.message for turn in turns]


def test_marks_a_cancelled_turn_without_a_reply_as_stopped(tmp_path: Path) -> None:
    cancelled = Event()
    cancelled.set()
    conversation = build_conversation(tmp_path, FakeClient([[]]))

    list(conversation.chat("Stop this one.", cancelled))

    turns = build_conversation(tmp_path, FakeClient([])).restore()
    assert turns[-1] == ("Stop this one.", [InterviewItem("stopped")])


def test_leaves_a_cancelled_turn_with_a_reply_unmarked(tmp_path: Path) -> None:
    cancelled = Event()
    conversation = build_conversation(tmp_path, FakeClient([partial_reply("Partial reply")]))
    events = conversation.chat("Stop this one.", cancelled)

    next(events)
    cancelled.set()
    list(events)

    turns = build_conversation(tmp_path, FakeClient([])).restore()
    assert turns[-1] == ("Stop this one.", [InterviewItem("assistant", "Partial reply")])


def test_clears_the_stopped_mark_on_the_next_turn(tmp_path: Path) -> None:
    cancelled = Event()
    cancelled.set()
    conversation = build_conversation(tmp_path, FakeClient([[], response(reply("Carrying on."))]))

    list(conversation.chat("Stop this one.", cancelled))
    list(conversation.chat("Carry on."))

    restarted = build_conversation(tmp_path, FakeClient([]))
    turns = restarted.restore()
    assert not restarted.session.stopped_turn
    assert [item.type for turn in turns for item in turn.items] == ["assistant"]


def test_leaves_valid_history_when_a_tool_call_is_cancelled(tmp_path: Path) -> None:
    cancelled = Event()
    client = FakeClient([response(call("switch", "switch_topic", topic="Delivery")), response(reply("Still works."))])
    conversation = build_conversation(tmp_path, client)
    events = conversation.chat("Switch topics.", cancelled)

    next(events)
    cancelled.set()
    list(events)
    list(conversation.chat("Continue."))

    context = cast("list[dict[str, object]]", client.responses.inputs[-1])
    assert conversation.interviewer.active_topic_id == "t1"
    assert {item["call_id"] for item in context if item.get("type") == "function_call"} == {"switch"}
    assert {item["call_id"] for item in context if item.get("type") == "function_call_output"} == {"switch"}


def test_rolls_back_the_changes_of_a_failed_turn(tmp_path: Path) -> None:
    conversation = build_conversation(
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
    list(conversation.chat("It has a terminal UI."))
    graph = conversation.interviewer.notebook.graph.model_dump()
    history = list(conversation.interviewer.history)
    active_topic_id = conversation.interviewer.active_topic_id
    notebook_file = conversation.notebook_file.read_bytes()

    with pytest.raises(RuntimeError, match="provider failed"):
        list(conversation.chat("Deploy it automatically."))

    assert conversation.interviewer.notebook.graph.model_dump() == {**graph, "next_note_id": "n3"}
    assert conversation.interviewer.history == [*history, {"role": "user", "content": "Deploy it automatically."}]
    assert conversation.interviewer.active_topic_id == active_topic_id
    assert conversation.notebook_file.read_bytes() == notebook_file.replace(b"next_note_id: n2", b"next_note_id: n3")


def test_restores_the_prompt_of_a_failed_turn(tmp_path: Path) -> None:
    conversation = build_conversation(tmp_path, FakeClient([failure("provider failed")]))

    with pytest.raises(RuntimeError, match="provider failed"):
        list(conversation.chat("Deploy it automatically."))

    turns = build_conversation(tmp_path, FakeClient([])).restore()
    assert turns[-1] == ("Deploy it automatically.", [])


def test_retries_a_failed_turn_after_restart(tmp_path: Path) -> None:
    conversation = build_conversation(tmp_path, FakeClient([failure("provider failed")]))

    with pytest.raises(RuntimeError, match="provider failed"):
        list(conversation.chat("Deploy it automatically."))

    restarted = build_conversation(tmp_path, FakeClient([response(reply("Retry succeeded."))]))
    restarted.restore()
    list(restarted.retry())

    turns = build_conversation(tmp_path, FakeClient([])).restore()
    assert [turn.message for turn in turns] == ["Deploy it automatically."]
    assert ("assistant", "Retry succeeded.") in [(item.type, item.text) for item in turns[-1].items]


def test_restores_the_error_of_a_failed_turn(tmp_path: Path) -> None:
    conversation = build_conversation(tmp_path, FakeClient([failure("provider failed")]))

    with pytest.raises(RuntimeError, match="provider failed"):
        list(conversation.chat("Deploy it automatically."))
    conversation.update_session(failed_turn_error="The provider failed.")

    turns = build_conversation(tmp_path, FakeClient([])).restore()
    assert turns[-1] == ("Deploy it automatically.", [InterviewItem("error", "The provider failed.")])


def test_clears_the_failed_turn_error_on_a_successful_retry(tmp_path: Path) -> None:
    conversation = build_conversation(
        tmp_path, FakeClient([failure("provider failed"), response(reply("Retry succeeded."))])
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        list(conversation.chat("Deploy it automatically."))
    conversation.update_session(failed_turn_error="The provider failed.")
    list(conversation.retry())

    restarted = build_conversation(tmp_path, FakeClient([]))
    turns = restarted.restore()
    assert restarted.session.failed_turn_error is None
    assert [item.type for item in turns[-1].items] == ["assistant"]


def test_clears_the_failed_turn_error_on_a_cancelled_retry(tmp_path: Path) -> None:
    cancelled = Event()
    conversation = build_conversation(
        tmp_path, FakeClient([failure("provider failed"), partial_reply("Partial reply")])
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        list(conversation.chat("Deploy it automatically."))
    conversation.update_session(failed_turn_error="The provider failed.")
    events = conversation.retry(cancelled)
    next(events)
    cancelled.set()
    list(events)

    restarted = build_conversation(tmp_path, FakeClient([]))
    turns = restarted.restore()
    assert restarted.session.failed_turn_error is None
    assert [item.type for item in turns[-1].items] == ["assistant"]


def test_clears_the_failed_turn_error_when_rewinding(tmp_path: Path) -> None:
    conversation = build_conversation(
        tmp_path, FakeClient([response(reply("What should it display?")), failure("provider")])
    )

    list(conversation.chat("It has a terminal UI."))
    with pytest.raises(RuntimeError, match="provider"):
        list(conversation.chat("Deploy it automatically."))
    conversation.update_session(failed_turn_error="The provider failed.")
    conversation.rewind(1)

    restarted = build_conversation(tmp_path, FakeClient([]))
    turns = restarted.restore()
    assert restarted.session.failed_turn_error is None
    assert [turn.message for turn in turns] == ["It has a terminal UI."]
    assert [item.type for item in turns[-1].items] == ["assistant"]


def test_removes_knowledge_captured_after_the_rewind_point(tmp_path: Path) -> None:
    conversation = build_conversation(
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
    list(conversation.chat("Deploy from main."))
    list(conversation.chat("Encrypt stored credentials."))
    list(conversation.chat("Charge monthly."))

    restarted = build_conversation(tmp_path, FakeClient([]))
    restarted.restore()
    restarted.rewind(1)

    reopened = build_conversation(tmp_path, FakeClient([]))
    turns = reopened.restore()
    graph = reopened.interviewer.notebook.graph

    assert reopened.interviewer.active_topic_id == "t2"
    assert {(topic.id, topic.name) for topic in graph.topics} == {("t1", "Project overview"), ("t2", "Delivery")}
    assert [(note.topic_id, note.text) for note in graph.notes] == [("t2", "Deploy from main.")]
    assert {"Encrypt stored credentials.", "Charge monthly."}.isdisjoint(turn.message for turn in turns)


def test_skips_failed_and_cancelled_tool_calls_when_rewinding(tmp_path: Path) -> None:
    cancelled = Event()
    conversation = build_conversation(
        tmp_path,
        FakeClient([
            response(call("failed", "switch_topic", topic="")),
            response(reply("That topic was invalid.")),
            response(call("cancelled", "switch_topic", topic="Delivery")),
            response(reply("Latest turn.")),
        ]),
    )
    list(conversation.chat("Try an invalid topic."))
    events = conversation.chat("Cancel this switch.", cancelled)
    next(events)
    cancelled.set()
    list(events)
    list(conversation.chat("Keep this only until rewind."))

    conversation.rewind(2)

    turns = build_conversation(tmp_path, FakeClient([])).restore()
    assert conversation.interviewer.active_topic_id == "t1"
    assert [(topic.id, topic.name) for topic in conversation.interviewer.notebook.graph.topics] == [
        ("t1", "Project overview")
    ]
    assert "Keep this only until rewind." not in [turn.message for turn in turns]


def test_skips_cancelled_tool_calls_when_rewinding_after_restart(tmp_path: Path) -> None:
    cancelled = Event()
    conversation = build_conversation(
        tmp_path, FakeClient([response(call("cancelled", "switch_topic", topic="Delivery"))])
    )
    events = conversation.chat("Cancel this switch.", cancelled)
    next(events)
    cancelled.set()
    list(events)

    restarted = build_conversation(tmp_path, FakeClient([response(reply("Latest turn."))]))
    restarted.restore()
    list(restarted.chat("Keep this only until rewind."))
    restarted.rewind(1)

    assert restarted.interviewer.active_topic_id == "t1"
    assert [(topic.id, topic.name) for topic in restarted.interviewer.notebook.graph.topics] == [
        ("t1", "Project overview")
    ]


def test_stores_the_session_as_compact_json(tmp_path: Path) -> None:
    conversation = build_conversation(tmp_path, FakeClient([response(reply("Noted."))]))

    list(conversation.chat("Deploy the project automatically."))

    content = conversation.session_file.read_text(encoding="utf-8")
    assert content == json.dumps(json.loads(content), separators=(",", ":"), ensure_ascii=False)


def test_explains_how_to_reset_an_invalid_session_file(tmp_path: Path) -> None:
    conversation = build_conversation(tmp_path, FakeClient([]))
    (tmp_path / paths.WORKSPACE_DIR).mkdir()
    conversation.session_file.write_text("not json")

    with pytest.raises(PersistenceError, match=r"Delete it .*--force"):
        conversation.restore()
