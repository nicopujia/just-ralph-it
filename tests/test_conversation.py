import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast

import pytest

from jri.core import paths
from jri.core.ai import Interviewer, ToolCallFinished, ToolCallStarted, TurnFinished, functional_analyst
from jri.core.conversation import Conversation
from jri.core.exceptions import PersistenceError
from tests.conftest import CreateRepository
from tests.doubles.generation import run_in_thread
from tests.doubles.openai import (
    FakeClient,
    Round,
    call,
    failure,
    partial_reply,
    rate_limited,
    rejection,
    reply,
    response,
    streamed_reply,
    thought,
)
from tests.doubles.settings import build_settings
from tests.doubles.specs_generation import (
    COMMIT,
    FINISHED_ROW,
    STARTED_ROW,
    THOUGHT,
    generate_blocked,
    generate_failing,
    generate_stopped,
    generate_succeeding,
    generate_thinking,
)
from tests.doubles.workspace import install_workspace


# Every generation runs in a process of its own, and a suite that
# spawned one would be reaching for a provider through a JRI nothing
# here can hand a double to.
@pytest.fixture(autouse=True)
def run_the_generation_here(monkeypatch: pytest.MonkeyPatch) -> None:
    run_in_thread(monkeypatch)


def build_conversation(client: FakeClient) -> Conversation:
    return Conversation(build_settings(client))


# A session recorded by a JRI whose tools have been renamed since.
def rename_recorded_calls(conversation: Conversation, name: str) -> None:
    session_file = conversation.workspace.session_file
    session = json.loads(session_file.read_text(encoding="utf-8"))
    for item in session["interview"]:
        if item.get("type") == "function_call":
            item["name"] = name
    session_file.write_text(json.dumps(session), encoding="utf-8")


# A session recorded by a JRI whose tool took a value this one refuses
# once it is running.
def rewrite_recorded_call(conversation: Conversation, name: str, **arguments: object) -> None:
    session_file = conversation.workspace.session_file
    session = json.loads(session_file.read_text(encoding="utf-8"))
    for item in session["interview"]:
        if item.get("type") == "function_call" and item["name"] == name:
            item["arguments"] = json.dumps({**json.loads(item["arguments"]), **arguments})
    session_file.write_text(json.dumps(session), encoding="utf-8")


# A session recorded by a JRI whose tool took a parameter this one no
# longer accepts.
def rename_recorded_parameter(conversation: Conversation, old: str, new: str) -> None:
    session_file = conversation.workspace.session_file
    session = json.loads(session_file.read_text(encoding="utf-8"))
    for item in session["interview"]:
        if item.get("type") != "function_call":
            continue
        arguments = json.loads(item["arguments"])
        if old in arguments:
            arguments[new] = arguments.pop(old)
            item["arguments"] = json.dumps(arguments)
    session_file.write_text(json.dumps(session), encoding="utf-8")


def test_leaves_the_workspace_untouched_until_a_command_reads_it(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    (tmp_path / paths.NOTEBOOK_FILE).unlink()

    Conversation(build_settings(FakeClient([])))

    assert not (tmp_path / paths.NOTEBOOK_FILE).exists()
    assert list((tmp_path / paths.LOGS_DIR).iterdir()) == []


def test_reads_the_notes_without_reaching_the_provider() -> None:
    unreachable = build_settings(FakeClient([])).model_copy(update={"llm": SimpleNamespace()})

    conversation = Conversation(unreachable)

    assert [topic.id for topic in conversation.notebook.graph.topics] == ["t1"]


@pytest.mark.parametrize(
    ("last_round", "finished"),
    [
        (streamed_reply("Noted."), TurnFinished("replied")),
        (response(), TurnFinished("empty")),
        (failure("provider failed"), TurnFinished("failed", "provider failed")),
        (
            rate_limited(code="insufficient_quota"),
            TurnFinished("exhausted", "Rate limit reached on tokens per min (TPM)."),
        ),
    ],
    ids=["replied", "empty", "failed", "exhausted"],
)
def test_ends_every_turn_with_its_rows_closed(last_round: object, finished: TurnFinished) -> None:
    conversation = build_conversation(
        FakeClient([response(call("switch", "switch_topic", topic="Delivery")), cast("Round", last_round)])
    )

    events = list(conversation.chat("Deploy from main."))

    assert [event.call_id for event in events if isinstance(event, ToolCallStarted)] == [
        event.call_id for event in events if isinstance(event, ToolCallFinished)
    ]
    assert events[-1] == finished


def test_closes_the_row_a_blocked_run_left_open(monkeypatch: pytest.MonkeyPatch) -> None:
    conversation = build_conversation(FakeClient([streamed_reply("Understood.")]))
    list(conversation.chat("Build a reporting CLI."))
    monkeypatch.setattr("jri.core.conversation.specs_generation.generate", generate_blocked)

    events = list(conversation.ralph())

    assert events[-2] == ToolCallFinished(STARTED_ROW.call_id, STARTED_ROW.label, "failed")
    assert events[-1] == TurnFinished("blocked", "Your project has uncommitted changes.")
    # The row the sweep closes is the one already written down, so a
    # turn that ended under an open row records it once.
    tool_items = [item for item in conversation.session.transcript[-1].items if item.type == "tool"]
    assert [(item.text, item.outcome) for item in tool_items] == [(STARTED_ROW.label, "failed")]


def test_keeps_a_turn_alive_when_a_provider_failure_hits_a_tool() -> None:
    conversation = build_conversation(
        FakeClient([
            response(call("explore", "explore", query="deployment options")),
            rejection(),
            streamed_reply("I could not look that up."),
        ])
    )

    events = list(conversation.chat("What are the deployment options?"))

    assert [(event.call_id, event.outcome) for event in events if isinstance(event, ToolCallFinished)] == [
        ("explore", "failed")
    ]
    assert events[-1] == TurnFinished("replied")


def test_restores_a_completed_interview_turn() -> None:
    conversation = build_conversation(
        FakeClient([
            response(
                call("switch", "switch_topic", topic="Delivery"),
                call("capture", "capture_notes", texts=["Deploy from the main branch."]),
            ),
            streamed_reply("How should failed deployments be handled?"),
        ])
    )

    list(conversation.chat("Deploy the project automatically."))

    restarted = build_conversation(FakeClient([]))
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


def test_groups_every_restored_item_under_the_prompt_that_caused_it() -> None:
    conversation = build_conversation(
        FakeClient([
            response(call("switch", "switch_topic", topic="Delivery")),
            streamed_reply("Noted."),
            response(call("capture", "capture_notes", texts=["Deploy from the main branch."])),
            streamed_reply("Anything else?"),
        ])
    )
    list(conversation.chat("First prompt."))
    list(conversation.chat("Second prompt."))

    turns = build_conversation(FakeClient([])).restore()

    assert [turn.message for turn in turns] == ["First prompt.", "Second prompt."]
    assert [item.type for item in turns[0].items] == ["tool", "assistant"]
    assert [item.text for item in turns[0].items] == ["Switched to Delivery", "Noted."]
    assert [item.type for item in turns[1].items] == ["tool", "assistant"]
    assert turns[1].items[-1].text == "Anything else?"


def test_restores_a_tool_call_that_failed_as_a_failure() -> None:
    conversation = build_conversation(
        FakeClient([
            response(call("edit", "edit_note", note_id="n9", text="Deploy from the main branch.")),
            streamed_reply("Which note did you mean?"),
        ])
    )
    list(conversation.chat("Fix that note."))

    turns = build_conversation(FakeClient([])).restore()

    item = turns[-1].items[0]
    assert (item.type, item.text, item.symbol, item.outcome, item.detail) == (
        "tool",
        "Edited note",
        "✏️",
        "failed",
        "Unknown note `n9`.",
    )


def test_restores_the_reasoning_a_turn_streamed() -> None:
    conversation = build_conversation(
        FakeClient([
            [
                SimpleNamespace(type="response.reasoning_summary_text.delta", delta="Weighing "),
                SimpleNamespace(type="response.reasoning_summary_text.delta", delta="the options."),
                *streamed_reply("How often does it deploy?"),
            ]
        ])
    )
    list(conversation.chat("It deploys automatically."))

    turns = build_conversation(FakeClient([])).restore()

    assert [(item.type, item.text) for item in turns[-1].items] == [
        ("reasoning", "Weighing the options."),
        ("assistant", "How often does it deploy?"),
    ]


def test_records_a_turn_in_the_order_its_events_arrived(monkeypatch: pytest.MonkeyPatch) -> None:
    conversation = build_conversation(
        FakeClient([streamed_reply("Understood."), streamed_reply("The specifications are in.")])
    )
    list(conversation.chat("Build a reporting CLI."))
    monkeypatch.setattr("jri.core.conversation.specs_generation.generate", generate_thinking)
    list(conversation.ralph())

    items = build_conversation(FakeClient([])).restore()[-1].items

    # The row reaches the screen when it opens, so a thought streamed
    # under it is read after it, not before.
    assert [(item.type, item.text) for item in items] == [
        ("assistant", "Understood."),
        ("tool", FINISHED_ROW.label),
        ("reasoning", THOUGHT.text),
        ("assistant", "The specifications are in."),
    ]
    assert [item.outcome for item in items if item.type == "tool"] == ["done"]


# Nothing records a row nested under a call, and the run of text above
# one still ends where it ends on screen: two thoughts a tool call
# stands between are two thoughts, not one sentence made of both.
def test_keeps_two_thoughts_a_nested_call_stands_between_apart() -> None:
    conversation = build_conversation(
        FakeClient([
            response(call("explore", "explore", query="deployment options")),
            [thought("Which files? "), *response(call("nested", "search_web", query="deployments"))],
            [thought("Read it."), *streamed_reply("Deployments run from main.")],
            streamed_reply("It deploys from main."),
        ])
    )

    list(conversation.chat("How does it deploy?"))

    items = build_conversation(FakeClient([])).restore()[-1].items
    assert [(item.type, item.text) for item in items] == [
        ("tool", "Explored deployment options"),
        ("reasoning", "Which files? "),
        ("reasoning", "Read it."),
        ("assistant", "It deploys from main."),
    ]


def test_leaves_the_rows_nested_under_a_call_out_of_the_recording() -> None:
    conversation = build_conversation(
        FakeClient([
            response(call("explore", "explore", query="deployment options")),
            response(call("nested", "search_web", query="deployments")),
            streamed_reply("Deployments run from main."),
            streamed_reply("It deploys from main."),
        ])
    )

    events = list(conversation.chat("How does it deploy?"))

    assert [event.depth for event in events if isinstance(event, ToolCallStarted)] == [0, 1]
    items = build_conversation(FakeClient([])).restore()[-1].items
    assert [(item.type, item.text) for item in items] == [
        ("tool", "Explored deployment options"),
        ("assistant", "It deploys from main."),
    ]


def test_records_a_row_the_session_was_written_under() -> None:
    conversation = build_conversation(
        FakeClient([response(call("switch", "switch_topic", topic="Delivery")), streamed_reply("Noted.")])
    )

    events = conversation.chat("Deploy from main.")
    next(events)
    # A `^t` pressed while a row spins saves the session under it.
    conversation.update_session(show_thinking_blocks=True)
    events.close()

    items = build_conversation(FakeClient([])).restore()[-1].items
    assert [(item.type, item.text, item.outcome) for item in items] == [("tool", "Switching to Delivery", None)]


def test_records_a_reply_the_provider_sent_whole() -> None:
    conversation = build_conversation(FakeClient([response(reply("How often does it deploy?"))]))

    list(conversation.chat("It deploys automatically."))

    turn = conversation.session.transcript[-1]
    assert [(item.type, item.text) for item in turn.items] == [("assistant", "How often does it deploy?")]
    assert turn.ending == "replied"


def test_restores_an_interview_under_the_prompt_of_the_running_process() -> None:
    conversation = build_conversation(FakeClient([streamed_reply("How often does it deploy?")]))
    list(conversation.chat("It deploys automatically."))

    restarted = build_conversation(FakeClient([]))
    restarted.restore()

    history = cast("list[dict[str, object]]", restarted.interviewer.history)
    assert [item["content"] for item in history if item.get("role") == "system"] == [restarted.interviewer.prompt]


def test_keeps_the_opening_message_of_a_session_saved_before_the_first_turn() -> None:
    build_conversation(FakeClient([])).update_session(show_thinking_blocks=True)
    client = FakeClient([streamed_reply("How often does it deploy?")])
    restarted = build_conversation(client)
    restarted.restore()

    list(restarted.chat("It deploys automatically."))

    context = cast("list[dict[str, object]]", client.responses.inputs[-1])
    assert Interviewer.FIRST_MESSAGE in [item.get("content") for item in context]


def test_restores_ralph_readiness_after_restart() -> None:
    conversation = build_conversation(
        FakeClient([response(call("ready", "offer_ralphing")), streamed_reply("Click Just Ralph It.")])
    )
    list(conversation.chat("We're ready."))

    restarted = build_conversation(FakeClient([]))
    restarted.restore()

    assert restarted.is_ready_to_ralph


def test_restores_the_thinking_blocks_preference_after_restart() -> None:
    conversation = build_conversation(FakeClient([]))
    conversation.update_session(show_thinking_blocks=True)

    restarted = build_conversation(FakeClient([]))
    restarted.restore()

    assert restarted.session.show_thinking_blocks is True


def test_rolls_back_ralph_readiness_when_the_turn_fails() -> None:
    conversation = build_conversation(
        FakeClient([
            response(call("ready", "offer_ralphing")),
            streamed_reply("Click Just Ralph It."),
            response(call("capture", "capture_notes", texts=["Ship every Friday."])),
            failure("provider failed"),
        ])
    )
    list(conversation.chat("We're ready."))

    events = list(conversation.chat("Actually, one more thing."))

    assert events[-1] == TurnFinished("failed", "provider failed")
    assert conversation.is_ready_to_ralph


def test_retires_the_offer_the_notes_moved_past() -> None:
    conversation = build_conversation(
        FakeClient([
            response(call("ready", "offer_ralphing")),
            streamed_reply("Click Just Ralph It."),
            response(call("capture", "capture_notes", texts=["Ship every Friday."])),
            streamed_reply("Noted."),
        ])
    )
    list(conversation.chat("We're ready."))

    list(conversation.chat("Actually, one more thing."))

    assert not conversation.is_ready_to_ralph


def test_keeps_the_offer_the_same_turn_kept_writing() -> None:
    conversation = build_conversation(
        FakeClient([
            response(
                call("ready", "offer_ralphing"),
                call("capture", "capture_notes", texts=["Deploy from main.", "Roll back on failure."]),
                call(
                    "connect", "connect_notes", connections=[{"source_id": "n1", "target_id": "n2", "label": "guards"}]
                ),
            ),
            streamed_reply("Click Just Ralph It."),
        ])
    )

    list(conversation.chat("We're ready."))

    assert conversation.is_ready_to_ralph


def test_keeps_the_offer_an_interrupted_generation_never_consumed(monkeypatch: pytest.MonkeyPatch) -> None:
    conversation = build_conversation(
        FakeClient([response(call("ready", "offer_ralphing")), streamed_reply("Click Just Ralph It.")])
    )
    list(conversation.chat("We're ready."))
    monkeypatch.setattr("jri.core.conversation.specs_generation.generate", generate_succeeding)

    events = conversation.ralph()
    next(events)
    events.close()

    restarted = build_conversation(FakeClient([]))
    restarted.restore()
    assert restarted.is_ready_to_ralph


def test_stops_a_generation_the_user_asked_to_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = Event()
    conversation = build_conversation(FakeClient([streamed_reply("Understood.")]))
    list(conversation.chat("Build a reporting CLI."))
    monkeypatch.setattr("jri.core.conversation.specs_generation.generate", generate_stopped)

    events = conversation.ralph(cancelled)
    next(events)
    cancelled.set()

    # No round is left for the interviewer, so a run that reported a
    # stopped generation to it would ask the provider for one.
    assert list(events) == [
        ToolCallFinished(STARTED_ROW.call_id, STARTED_ROW.label, "stopped"),
        TurnFinished("stopped"),
    ]
    assert conversation.session.transcript[-1].items[-1].outcome == "stopped"


def test_keeps_the_offer_a_stopped_generation_never_spent(monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = Event()
    conversation = build_conversation(
        FakeClient([response(call("ready", "offer_ralphing")), streamed_reply("Click Just Ralph It.")])
    )
    list(conversation.chat("We're ready."))
    monkeypatch.setattr("jri.core.conversation.specs_generation.generate", generate_stopped)

    events = conversation.ralph(cancelled)
    next(events)
    cancelled.set()
    list(events)

    restarted = build_conversation(FakeClient([]))
    restarted.restore()
    assert restarted.is_ready_to_ralph


def test_drops_the_offer_a_generation_consumed(monkeypatch: pytest.MonkeyPatch) -> None:
    conversation = build_conversation(
        FakeClient([
            response(call("ready", "offer_ralphing")),
            streamed_reply("Click Just Ralph It."),
            streamed_reply("The specifications are in."),
        ])
    )
    list(conversation.chat("We're ready."))
    monkeypatch.setattr("jri.core.conversation.specs_generation.generate", generate_succeeding)

    list(conversation.ralph())

    assert not conversation.is_ready_to_ralph


def test_asks_the_interviewer_about_the_ambiguities_ralph_found(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    create_repository(tmp_path)
    install_workspace(tmp_path)
    ambiguity = "Choose whether output is JSON or plain text."
    client = FakeClient(
        [streamed_reply("Understood."), streamed_reply("Should the output be JSON or plain text?")],
        parsed=[
            functional_analyst.Output(
                result=functional_analyst.Ambiguities(outcome="ambiguities", ambiguities=[ambiguity])
            )
        ],
    )
    conversation = build_conversation(client)
    list(conversation.chat("Build a reporting CLI."))

    list(conversation.ralph())

    assert any(ambiguity in item.get("content", "") for item in conversation.session.interview)
    restarted = build_conversation(FakeClient([]))
    turns = restarted.restore()
    assert ("assistant", "Should the output be JSON or plain text?") in [
        (item.type, item.text) for item in turns[-1].items
    ]


def test_restores_a_just_ralph_it_run(monkeypatch: pytest.MonkeyPatch) -> None:
    conversation = build_conversation(
        FakeClient([streamed_reply("Understood."), streamed_reply("The specifications are in.")])
    )
    list(conversation.chat("Build a reporting CLI."))
    monkeypatch.setattr("jri.core.conversation.specs_generation.generate", generate_succeeding)
    list(conversation.ralph())

    turns = build_conversation(FakeClient([])).restore()

    assert [(item.type, item.text) for item in turns[-1].items] == [
        ("assistant", "Understood."),
        ("tool", "Saved the specifications to your project"),
        ("assistant", "The specifications are in."),
    ]
    assert turns[-1].ending == "replied"


def test_ends_a_run_the_process_before_it_did_not_stay_for(monkeypatch: pytest.MonkeyPatch) -> None:
    conversation = build_conversation(FakeClient([streamed_reply("Understood.")]))
    list(conversation.chat("Build a reporting CLI."))
    monkeypatch.setattr("jri.core.conversation.specs_generation.generate", generate_succeeding)
    events = conversation.ralph()
    next(events)
    # A `^t` pressed while the run is going saves the session under it.
    conversation.update_session(show_thinking_blocks=True)
    events.close()

    restarted = build_conversation(FakeClient([streamed_reply("The specifications are in.")]))
    restarted.restore()
    assert restarted.pending_generation
    folded = list(restarted.ralph())

    # The turn ends in the process that reads the ending, with every
    # row the run opened closed, and the rows are read once: while a
    # run is in flight its record is the journal, so the session holds
    # the turn as it stood before it rather than half of it twice.
    assert [event.call_id for event in folded if isinstance(event, ToolCallStarted)] == [
        event.call_id for event in folded if isinstance(event, ToolCallFinished)
    ]
    assert [event for event in folded if isinstance(event, TurnFinished)] == [TurnFinished("replied")]
    assert [(item.type, item.text) for item in restarted.session.transcript[-1].items] == [
        ("assistant", "Understood."),
        ("tool", FINISHED_ROW.label),
        ("assistant", "The specifications are in."),
    ]


def test_reports_a_finished_generation_without_barring_the_next_one(monkeypatch: pytest.MonkeyPatch) -> None:
    conversation = build_conversation(FakeClient([streamed_reply("Understood."), streamed_reply("All set.")]))
    list(conversation.chat("Build a reporting CLI."))
    monkeypatch.setattr("jri.core.conversation.specs_generation.generate", generate_succeeding)

    list(conversation.ralph())

    reports = [item["content"] for item in conversation.session.interview[1:] if item.get("role") == "system"]
    assert reports == [f"Specification generation succeeded in Git commit {COMMIT}."]
    assert not conversation.is_ready_to_ralph


def test_rolls_back_the_notes_of_a_failed_reply_after_ralphing(monkeypatch: pytest.MonkeyPatch) -> None:
    conversation = build_conversation(
        FakeClient([
            streamed_reply("Understood."),
            response(call("capture", "capture_notes", texts=["Ship every Friday."])),
            failure("provider failed"),
        ])
    )
    list(conversation.chat("Build a reporting CLI."))
    monkeypatch.setattr("jri.core.conversation.specs_generation.generate", generate_succeeding)

    events = list(conversation.ralph())

    assert events[-1] == TurnFinished("failed", "provider failed")
    reopened = build_conversation(FakeClient([]))
    reopened.restore()
    assert [note.text for note in reopened.notebook.graph.notes] == []


def test_retries_the_reply_a_generation_report_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    conversation = build_conversation(
        FakeClient([streamed_reply("Understood."), failure("provider failed"), streamed_reply("The specs are in.")])
    )
    list(conversation.chat("Build a reporting CLI."))
    monkeypatch.setattr("jri.core.conversation.specs_generation.generate", generate_succeeding)
    list(conversation.ralph())

    events = list(conversation.retry())

    assert events[-1] == TurnFinished("replied")
    # Sending the last prompt again instead would drop the report the
    # reply is about out of the model's history.
    reports = [item["content"] for item in conversation.session.interview[1:] if item.get("role") == "system"]
    assert reports == [f"Specification generation succeeded in Git commit {COMMIT}."]
    assert conversation.session.transcript[-1].message == "Build a reporting CLI."


def test_runs_a_failed_generation_again_instead_of_the_message_before_it(monkeypatch: pytest.MonkeyPatch) -> None:
    conversation = build_conversation(
        FakeClient([
            response(call("ready", "offer_ralphing")),
            streamed_reply("Click Just Ralph It."),
            streamed_reply("The specifications are in."),
        ])
    )
    list(conversation.chat("Build a reporting CLI."))
    monkeypatch.setattr("jri.core.conversation.specs_generation.generate", generate_failing)
    list(conversation.ralph())
    monkeypatch.setattr("jri.core.conversation.specs_generation.generate", generate_succeeding)

    events = list(conversation.retry())

    assert events[-1] == TurnFinished("replied")
    reports = [item["content"] for item in conversation.session.interview[1:] if item.get("role") == "system"]
    assert reports == [f"Specification generation succeeded in Git commit {COMMIT}."]
    # Sending the prompt again instead would re-run the interview turn
    # the run reported into, leaving the user to ask for the run a
    # second time from a reply they had already read.
    assert [(item.type, item.text) for item in conversation.session.transcript[-1].items] == [
        ("tool", "Offered Just Ralph It"),
        ("assistant", "Click Just Ralph It."),
        ("tool", STARTED_ROW.label),
        ("tool", FINISHED_ROW.label),
        ("assistant", "The specifications are in."),
    ]


def test_runs_a_failed_generation_again_after_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    conversation = build_conversation(FakeClient([streamed_reply("Click Just Ralph It.")]))
    list(conversation.chat("Build a reporting CLI."))
    monkeypatch.setattr("jri.core.conversation.specs_generation.generate", generate_failing)
    list(conversation.ralph())

    restarted = build_conversation(FakeClient([streamed_reply("The specifications are in.")]))
    restarted.restore()
    monkeypatch.setattr("jri.core.conversation.specs_generation.generate", generate_succeeding)
    events = list(restarted.retry())

    assert events[-1] == TurnFinished("replied")
    reports = [item["content"] for item in restarted.session.interview[1:] if item.get("role") == "system"]
    assert reports == [f"Specification generation succeeded in Git commit {COMMIT}."]


def test_replies_again_after_the_retry_of_a_reply_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    conversation = build_conversation(
        FakeClient([
            streamed_reply("Understood."),
            failure("provider failed"),
            failure("provider failed again"),
            streamed_reply("The specifications are in."),
        ])
    )
    list(conversation.chat("Build a reporting CLI."))
    monkeypatch.setattr("jri.core.conversation.specs_generation.generate", generate_succeeding)
    list(conversation.ralph())
    list(conversation.retry())

    events = list(conversation.retry())

    assert events[-1] == TurnFinished("replied")
    # Retrying it as a message instead would drop the report the reply
    # is about and put what JRI wrote in it to the model as something
    # the user had typed.
    assert [item.get("content") for item in conversation.session.interview if item.get("role") == "user"] == [
        "Build a reporting CLI."
    ]


def test_rejects_a_session_saved_before_a_turn_recorded_its_work() -> None:
    conversation = build_conversation(FakeClient([streamed_reply("Understood.")]))
    list(conversation.chat("Build a reporting CLI."))
    stored = json.loads(conversation.workspace.session_file.read_bytes())
    for turn in stored["transcript"]:
        del turn["work"]
    conversation.workspace.session_file.write_bytes(json.dumps(stored).encode())

    # A run that failed and a message that failed leave the interview
    # in the same state, so a turn that never recorded which it was is
    # one nothing can ask for again.
    with pytest.raises(PersistenceError, match=r"Delete it .*--force"):
        build_conversation(FakeClient([])).restore()


def test_rejects_a_session_file_that_is_not_utf_8() -> None:
    conversation = build_conversation(FakeClient([streamed_reply("Understood.")]))
    list(conversation.chat("Build a reporting CLI."))
    conversation.workspace.session_file.write_bytes(b'{"active_topic_id": "\xff"}')

    with pytest.raises(PersistenceError, match=r"Delete it .*--force"):
        build_conversation(FakeClient([])).restore()


def test_restores_a_cancelled_interview_turn() -> None:
    cancelled = Event()
    conversation = build_conversation(FakeClient([partial_reply("Partial reply")]))
    events = conversation.chat("Keep this prompt.", cancelled)

    next(events)
    cancelled.set()
    list(events)

    turns = build_conversation(FakeClient([])).restore()
    cancelled_turn = next(turn for turn in turns if turn.message == "Keep this prompt.")
    assert ("assistant", "Partial reply") in [(item.type, item.text) for item in cancelled_turn.items]


def test_keeps_a_cancelled_reply_in_the_model_context() -> None:
    cancelled = Event()
    client = FakeClient([partial_reply("Partial reply"), streamed_reply("Next reply")])
    conversation = build_conversation(client)
    events = conversation.chat("Keep this prompt.", cancelled)
    next(events)
    cancelled.set()
    list(events)

    list(conversation.chat("Continue."))

    context = cast("list[dict[str, object]]", client.responses.inputs[-1])
    assert {item["content"] for item in context if "content" in item} >= {"Keep this prompt.", "Partial reply"}


def test_keeps_the_prompt_of_a_cancelled_turn_without_a_reply() -> None:
    cancelled = Event()
    cancelled.set()
    conversation = build_conversation(FakeClient([[]]))

    list(conversation.chat("Keep this prompt.", cancelled))

    restarted = build_conversation(FakeClient([]))
    turns = restarted.restore()
    assert "Keep this prompt." in [turn.message for turn in turns]


def test_marks_a_cancelled_turn_without_a_reply_as_stopped() -> None:
    cancelled = Event()
    cancelled.set()
    conversation = build_conversation(FakeClient([[]]))

    list(conversation.chat("Stop this one.", cancelled))

    turns = build_conversation(FakeClient([])).restore()
    assert (turns[-1].message, turns[-1].items, turns[-1].ending) == ("Stop this one.", [], "stopped")


def test_leaves_a_cancelled_turn_with_a_reply_unmarked() -> None:
    cancelled = Event()
    conversation = build_conversation(FakeClient([partial_reply("Partial reply")]))
    events = conversation.chat("Stop this one.", cancelled)

    next(events)
    cancelled.set()
    list(events)

    turns = build_conversation(FakeClient([])).restore()
    assert [(item.type, item.text) for item in turns[-1].items] == [("assistant", "Partial reply")]
    assert turns[-1].ending == "replied"


def test_keeps_the_stopped_mark_on_its_turn_when_a_later_one_ends() -> None:
    cancelled = Event()
    cancelled.set()
    conversation = build_conversation(FakeClient([[], streamed_reply("Carrying on.")]))

    list(conversation.chat("Stop this one.", cancelled))
    list(conversation.chat("Carry on."))

    turns = build_conversation(FakeClient([])).restore()
    assert [(turn.message, turn.ending) for turn in turns] == [("Stop this one.", "stopped"), ("Carry on.", "replied")]


def test_clears_the_stopped_mark_when_its_turn_is_sent_again() -> None:
    cancelled = Event()
    cancelled.set()
    conversation = build_conversation(FakeClient([[], response(reply(""))]))
    list(conversation.chat("Stop this one.", cancelled))

    list(conversation.retry())

    turns = build_conversation(FakeClient([])).restore()
    assert [(turn.message, turn.items, turn.ending) for turn in turns] == [("Stop this one.", [], "empty")]


def test_keeps_the_stopped_mark_on_the_cancelled_turn_when_the_next_one_fails() -> None:
    cancelled = Event()
    cancelled.set()
    conversation = build_conversation(FakeClient([[], failure("provider failed")]))
    list(conversation.chat("Stop this one.", cancelled))

    list(conversation.chat("Deploy it automatically."))

    turns = build_conversation(FakeClient([])).restore()
    assert [(turn.message, turn.ending) for turn in turns] == [
        ("Stop this one.", "stopped"),
        ("Deploy it automatically.", "failed"),
    ]


def test_leaves_valid_history_when_a_tool_call_is_cancelled() -> None:
    cancelled = Event()
    client = FakeClient([response(call("switch", "switch_topic", topic="Delivery")), streamed_reply("Still works.")])
    conversation = build_conversation(client)
    events = conversation.chat("Switch topics.", cancelled)

    next(events)
    cancelled.set()
    list(events)
    list(conversation.chat("Continue."))

    context = cast("list[dict[str, object]]", client.responses.inputs[-1])
    assert conversation.interviewer.active_topic_id == "t1"
    assert {item["call_id"] for item in context if item.get("type") == "function_call"} == {"switch"}
    assert {item["call_id"] for item in context if item.get("type") == "function_call_output"} == {"switch"}


def test_rolls_back_the_changes_of_a_failed_turn() -> None:
    conversation = build_conversation(
        FakeClient([
            response(call("first-capture", "capture_notes", texts=["The project has a terminal UI."])),
            streamed_reply("What should it display?"),
            response(
                call("switch", "switch_topic", topic="Delivery"),
                call("second-capture", "capture_notes", texts=["Deploy automatically."]),
            ),
            failure("provider failed"),
        ])
    )
    list(conversation.chat("It has a terminal UI."))
    graph = conversation.interviewer.notebook.graph.model_dump()
    history = list(conversation.interviewer.history)
    active_topic_id = conversation.interviewer.active_topic_id
    notebook_file = conversation.workspace.notebook_file.read_bytes()

    events = list(conversation.chat("Deploy it automatically."))

    assert events[-1] == TurnFinished("failed", "provider failed")
    assert conversation.interviewer.notebook.graph.model_dump() == {**graph, "next_note_id": "n3"}
    assert conversation.interviewer.history == [*history, {"role": "user", "content": "Deploy it automatically."}]
    assert conversation.interviewer.active_topic_id == active_topic_id
    assert conversation.workspace.notebook_file.read_bytes() == notebook_file.replace(
        b"next_note_id: n2", b"next_note_id: n3"
    )


def test_restores_the_ending_of_a_turn_that_failed() -> None:
    conversation = build_conversation(FakeClient([failure("provider failed")]))

    list(conversation.chat("Deploy it automatically."))

    turns = build_conversation(FakeClient([])).restore()
    assert (turns[-1].message, turns[-1].items) == ("Deploy it automatically.", [])
    assert (turns[-1].ending, turns[-1].detail) == ("failed", "provider failed")


def test_retries_a_failed_turn_after_restart() -> None:
    conversation = build_conversation(FakeClient([failure("provider failed")]))
    list(conversation.chat("Deploy it automatically."))

    restarted = build_conversation(FakeClient([streamed_reply("Retry succeeded.")]))
    restarted.restore()
    list(restarted.retry())

    turns = build_conversation(FakeClient([])).restore()
    assert [turn.message for turn in turns] == ["Deploy it automatically."]
    assert [(item.type, item.text) for item in turns[-1].items] == [("assistant", "Retry succeeded.")]
    assert turns[-1].ending == "replied"


def test_resends_the_prompt_when_retrying_a_turn_that_brought_no_reply() -> None:
    conversation = build_conversation(FakeClient([response(reply("")), streamed_reply("Retry succeeded.")]))
    list(conversation.chat("Deploy it automatically."))

    list(conversation.retry())

    turns = build_conversation(FakeClient([])).restore()
    assert [turn.message for turn in turns] == ["Deploy it automatically."]
    assert ("assistant", "Retry succeeded.") in [(item.type, item.text) for item in turns[-1].items]


# One prompt is the case where the newest, the oldest and the only one
# are the same item, so it cannot tell which of them a retry re-sends.
def test_retries_the_newest_of_several_prompts() -> None:
    client = FakeClient([
        streamed_reply("What should it report on?"),
        failure("provider failed"),
        streamed_reply("Retry succeeded."),
    ])
    conversation = build_conversation(client)
    list(conversation.chat("Build a reporting CLI."))
    list(conversation.chat("Deploy it automatically."))

    list(conversation.retry())

    context = cast("list[dict[str, object]]", client.responses.inputs[-1])
    assert [item["content"] for item in context if item.get("role") == "user"] == [
        "Build a reporting CLI.",
        "Deploy it automatically.",
    ]
    assert context[-1]["content"] == "Deploy it automatically."


def test_removes_knowledge_captured_after_the_rewind_point() -> None:
    conversation = build_conversation(
        FakeClient([
            response(
                call("delivery-switch", "switch_topic", topic="Delivery"),
                call("delivery-capture", "capture_notes", texts=["Deploy from main."]),
            ),
            streamed_reply("Delivery captured."),
            response(
                call("security-switch", "switch_topic", topic="Security"),
                call("security-capture", "capture_notes", texts=["Encrypt stored credentials."]),
            ),
            streamed_reply("Security captured."),
            response(
                call("billing-switch", "switch_topic", topic="Billing"),
                call("billing-capture", "capture_notes", texts=["Charge monthly."]),
            ),
            streamed_reply("Billing captured."),
        ])
    )
    list(conversation.chat("Deploy from main."))
    list(conversation.chat("Encrypt stored credentials."))
    list(conversation.chat("Charge monthly."))

    restarted = build_conversation(FakeClient([]))
    restarted.restore()
    restarted.rewind(1)

    reopened = build_conversation(FakeClient([]))
    turns = reopened.restore()
    graph = reopened.interviewer.notebook.graph

    assert reopened.interviewer.active_topic_id == "t2"
    assert {(topic.id, topic.name) for topic in graph.topics} == {("t1", "Project overview"), ("t2", "Delivery")}
    assert [(note.topic_id, note.text) for note in graph.notes] == [("t2", "Deploy from main.")]
    assert {"Encrypt stored credentials.", "Charge monthly."}.isdisjoint(turn.message for turn in turns)


def test_skips_failed_and_cancelled_tool_calls_when_rewinding() -> None:
    cancelled = Event()
    conversation = build_conversation(
        FakeClient([
            response(call("failed", "switch_topic", topic="")),
            streamed_reply("That topic was invalid."),
            response(call("cancelled", "switch_topic", topic="Delivery")),
            streamed_reply("Latest turn."),
        ])
    )
    list(conversation.chat("Try an invalid topic."))
    events = conversation.chat("Cancel this switch.", cancelled)
    next(events)
    cancelled.set()
    list(events)
    list(conversation.chat("Keep this only until rewind."))

    conversation.rewind(2)

    turns = build_conversation(FakeClient([])).restore()
    assert conversation.interviewer.active_topic_id == "t1"
    assert [(topic.id, topic.name) for topic in conversation.interviewer.notebook.graph.topics] == [
        ("t1", "Project overview")
    ]
    assert "Keep this only until rewind." not in [turn.message for turn in turns]


def test_skips_cancelled_tool_calls_when_rewinding_after_restart() -> None:
    cancelled = Event()
    conversation = build_conversation(FakeClient([response(call("cancelled", "switch_topic", topic="Delivery"))]))
    events = conversation.chat("Cancel this switch.", cancelled)
    next(events)
    cancelled.set()
    list(events)

    restarted = build_conversation(FakeClient([streamed_reply("Latest turn.")]))
    restarted.restore()
    list(restarted.chat("Keep this only until rewind."))
    restarted.rewind(1)

    assert restarted.interviewer.active_topic_id == "t1"
    assert [(topic.id, topic.name) for topic in restarted.interviewer.notebook.graph.topics] == [
        ("t1", "Project overview")
    ]


def test_keeps_the_connections_between_replayed_notes_when_rewinding() -> None:
    conversation = build_conversation(
        FakeClient([
            response(
                call("capture", "capture_notes", texts=["Deploy from main.", "Roll back on failure."]),
                call(
                    "connect", "connect_notes", connections=[{"source_id": "n1", "target_id": "n2", "label": "guards"}]
                ),
            ),
            streamed_reply("Delivery captured."),
            response(call("billing-capture", "capture_notes", texts=["Charge monthly."])),
            streamed_reply("Billing captured."),
        ])
    )
    list(conversation.chat("Deploy from main."))
    list(conversation.chat("Charge monthly."))

    conversation.rewind(1)

    graph = build_conversation(FakeClient([])).notebook.graph
    texts = {note.id: note.text for note in graph.notes}
    assert sorted(texts.values()) == ["Deploy from main.", "Roll back on failure."]
    assert [(texts[item.source_id], item.label, texts[item.target_id]) for item in graph.connections] == [
        ("Deploy from main.", "guards", "Roll back on failure.")
    ]


def test_keeps_the_offer_made_before_the_rewind_point() -> None:
    conversation = build_conversation(
        FakeClient([
            response(call("ready", "offer_ralphing")),
            streamed_reply("Click Just Ralph It."),
            streamed_reply("Noted."),
        ])
    )
    list(conversation.chat("We're ready."))
    list(conversation.chat("One more thing."))

    conversation.rewind(1)

    restarted = build_conversation(FakeClient([]))
    restarted.restore()
    assert restarted.is_ready_to_ralph


# A rewind puts the notes back the way they stood before the turns it
# drops, so specifications a run drafted from the notes it dropped are
# work about a conversation that no longer happened.
def test_drops_the_draft_a_rewind_moved_past() -> None:
    conversation = build_conversation(FakeClient([streamed_reply("Noted."), streamed_reply("Also noted.")]))
    list(conversation.chat("We're ready."))
    list(conversation.chat("One more thing."))
    draft = conversation.workspace.draft_file
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("diff --git a/x b/x\n", encoding="utf-8")

    conversation.rewind(1)

    assert not draft.exists()


def test_skips_tool_calls_that_are_not_replayed_when_rewinding() -> None:
    # Only the rounds a run without a second exploration needs, so
    # replaying `explore` would starve the turn after the rewind.
    conversation = build_conversation(
        FakeClient([
            response(call("explore", "explore", query="deployment options")),
            streamed_reply("Deployments run from the main branch."),
            streamed_reply("Here is what I found."),
            streamed_reply("Understood."),
            streamed_reply("Anything else?"),
        ])
    )
    list(conversation.chat("What are the deployment options?"))
    list(conversation.chat("Thanks."))

    conversation.rewind(1)
    list(conversation.chat("Let's talk about billing."))

    turns = build_conversation(FakeClient([])).restore()
    assert [turn.message for turn in turns] == ["What are the deployment options?", "Let's talk about billing."]
    assert [(item.type, item.text) for item in turns[-1].items] == [("assistant", "Anything else?")]


def test_refuses_a_rewind_through_a_tool_this_version_no_longer_has() -> None:
    conversation = build_conversation(
        FakeClient([
            response(call("capture", "capture_notes", texts=["Deploy from main."])),
            streamed_reply("Delivery captured."),
            streamed_reply("Noted."),
        ])
    )
    list(conversation.chat("Deploy from main."))
    list(conversation.chat("One more thing."))
    rename_recorded_calls(conversation, "record_notes")

    restarted = build_conversation(FakeClient([]))
    restarted.restore()

    with pytest.raises(PersistenceError, match="record_notes"):
        restarted.rewind(1)

    reopened = build_conversation(FakeClient([]))
    assert [turn.message for turn in reopened.restore()] == ["Deploy from main.", "One more thing."]
    assert [note.text for note in reopened.notebook.graph.notes] == ["Deploy from main."]


def test_refuses_a_rewind_through_a_call_this_version_no_longer_accepts() -> None:
    conversation = build_conversation(
        FakeClient([
            response(call("capture", "capture_notes", texts=["Deploy from main."])),
            streamed_reply("Delivery captured."),
            streamed_reply("Noted."),
        ])
    )
    list(conversation.chat("Deploy from main."))
    list(conversation.chat("One more thing."))
    rename_recorded_parameter(conversation, "texts", "notes")

    restarted = build_conversation(FakeClient([]))
    restarted.restore()

    # Decided before the notebook is touched at all, so what it reports
    # is the call this version cannot make rather than a failure it
    # went and provoked.
    with pytest.raises(PersistenceError, match="`capture_notes` in a way this version of JRI cannot make again"):
        restarted.rewind(1)

    reopened = build_conversation(FakeClient([]))
    assert [turn.message for turn in reopened.restore()] == ["Deploy from main.", "One more thing."]
    assert [note.text for note in reopened.notebook.graph.notes] == ["Deploy from main."]


def test_refuses_a_rewind_whose_replay_fails_inside_a_tool_that_took_the_call() -> None:
    conversation = build_conversation(
        FakeClient([
            response(
                call("switch", "switch_topic", topic="Delivery"),
                call("capture", "capture_notes", texts=["Deploy from main."]),
                call("ready", "offer_ralphing"),
                call("trash", "update_topic", topic_id="t2", status="trashed"),
            ),
            streamed_reply("Delivery trashed."),
            streamed_reply("Noted."),
        ])
    )
    list(conversation.chat("Deploy from main."))
    list(conversation.chat("One more thing."))
    rewrite_recorded_call(conversation, "update_topic", topic_id="t1")

    restarted = build_conversation(
        FakeClient([response(call("more", "capture_notes", texts=["Roll back on failure."])), streamed_reply("Noted.")])
    )
    restarted.restore()

    with pytest.raises(PersistenceError, match="cannot be trashed"):
        restarted.rewind(1)
    list(restarted.chat("Carry on."))

    reopened = build_conversation(FakeClient([]))
    turns = reopened.restore()
    offer = reopened.session.ready_graph
    assert [turn.message for turn in turns] == ["Deploy from main.", "One more thing.", "Carry on."]
    assert [item["content"] for item in reopened.session.interview if item.get("role") == "user"] == [
        "Deploy from main.",
        "One more thing.",
        "Carry on.",
    ]
    assert [(topic.id, topic.status) for topic in reopened.notebook.graph.topics] == [("t1", "open"), ("t2", "trashed")]
    assert [note.text for note in reopened.notebook.graph.notes] == ["Deploy from main.", "Roll back on failure."]
    assert reopened.interviewer.active_topic_id == "t1"
    # The offer the refused rewind replayed belongs to no turn of this
    # conversation, so what stands is the one the user was looking at.
    assert offer is not None
    assert [note.text for note in offer.notes] == ["Deploy from main."]
    assert not reopened.is_ready_to_ralph


def test_rewinds_through_a_call_only_a_tool_it_never_replays_no_longer_accepts() -> None:
    conversation = build_conversation(
        FakeClient([
            response(
                call("read", "read_notes", query={"text": "delivery"}),
                call("capture", "capture_notes", texts=["Deploy from main."]),
            ),
            streamed_reply("Delivery captured."),
            streamed_reply("Noted."),
        ])
    )
    list(conversation.chat("Deploy from main."))
    list(conversation.chat("One more thing."))
    rename_recorded_parameter(conversation, "query", "filter")

    restarted = build_conversation(FakeClient([]))
    restarted.restore()
    restarted.rewind(1)

    reopened = build_conversation(FakeClient([]))
    assert [turn.message for turn in reopened.restore()] == ["Deploy from main."]
    assert [note.text for note in reopened.notebook.graph.notes] == ["Deploy from main."]


def test_rewinds_through_a_failed_call_to_a_tool_this_version_no_longer_has() -> None:
    conversation = build_conversation(
        FakeClient([
            response(call("failed", "switch_topic", topic="")),
            streamed_reply("That topic was invalid."),
            streamed_reply("Noted."),
        ])
    )
    list(conversation.chat("Switch to nothing."))
    list(conversation.chat("One more thing."))
    rename_recorded_calls(conversation, "change_topic")

    restarted = build_conversation(FakeClient([]))
    restarted.restore()
    restarted.rewind(1)

    reopened = build_conversation(FakeClient([]))
    assert [turn.message for turn in reopened.restore()] == ["Switch to nothing."]
    assert [topic.name for topic in reopened.notebook.graph.topics] == ["Project overview"]


def test_rewinds_to_before_a_tool_this_version_no_longer_has() -> None:
    conversation = build_conversation(
        FakeClient([
            streamed_reply("Tell me more."),
            response(call("capture", "capture_notes", texts=["Deploy from main."])),
            streamed_reply("Delivery captured."),
            streamed_reply("Noted."),
        ])
    )
    list(conversation.chat("Build a reporting CLI."))
    list(conversation.chat("Deploy from main."))
    rename_recorded_calls(conversation, "record_notes")

    restarted = build_conversation(FakeClient([]))
    restarted.restore()
    restarted.rewind(0)

    reopened = build_conversation(FakeClient([]))
    assert reopened.restore() == []
    assert reopened.notebook.graph.notes == []


def test_stores_the_session_as_compact_json() -> None:
    conversation = build_conversation(FakeClient([streamed_reply("Noted.")]))

    list(conversation.chat("Deploy the project automatically."))

    content = conversation.workspace.session_file.read_text(encoding="utf-8")
    assert content == json.dumps(json.loads(content), separators=(",", ":"), ensure_ascii=False)


def test_explains_how_to_reset_an_invalid_session_file(tmp_path: Path) -> None:
    conversation = build_conversation(FakeClient([]))
    (tmp_path / paths.WORKSPACE_DIR).mkdir()
    conversation.workspace.session_file.write_text("not json")

    with pytest.raises(PersistenceError, match=r"Delete it .*--force"):
        conversation.restore()


def test_reports_a_session_that_cannot_be_written() -> None:
    conversation = build_conversation(FakeClient([]))
    conversation.workspace.session_file.mkdir(parents=True)
    (conversation.workspace.session_file / "blocker").write_text("taken")

    with pytest.raises(PersistenceError, match="Could not save the session file"):
        conversation.update_session(show_thinking_blocks=True)

    assert not conversation.session.show_thinking_blocks
