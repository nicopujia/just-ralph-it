from pathlib import Path
from typing import cast

import pytest

from jri.core.ai import Interviewer, ToolCallStarted, ToolOutput
from jri.core.notes import Connection, Notebook
from tests.doubles.models import serve_catalog
from tests.doubles.openai import FakeClient, call, failure, partial_reply, response, streamed_reply
from tests.doubles.settings import build_settings

CONNECTION = Connection(source_id="n1", target_id="n2", label="constrains")
TURNS = 12


def build_interviewer(path: Path, client: FakeClient | None = None) -> Interviewer:
    return Interviewer(build_settings(client or FakeClient([])), Notebook(path / "notebook.yaml"))


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


def test_keeps_the_whole_history_when_it_fits_the_budget(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)
    interviewer.history.extend([{"role": "user", "content": "Hi."}, {"role": "assistant", "content": "Hello."}])

    context = interviewer.get_context()

    assert context[0] == interviewer.history[0]
    assert context[2:] == interviewer.history[1:]


def test_never_leaves_a_tool_output_without_its_call_in_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    serve_catalog(monkeypatch, {"test": {"limit": {"context": 1}}})
    interviewer = build_interviewer(tmp_path)
    for index in range(TURNS):
        interviewer.history.extend([
            {"role": "user", "content": f"Question {index}"},
            {"type": "function_call", "call_id": f"c{index}", "name": "read_notes", "arguments": "{}"},
            {"type": "function_call_output", "call_id": f"c{index}", "output": "No notes found."},
            {"role": "assistant", "content": f"Answer {index}"},
        ])

    context = cast("list[dict[str, object]]", interviewer.get_context())

    calls = {item["call_id"] for item in context if item.get("type") == "function_call"}
    outputs = {item["call_id"] for item in context if item.get("type") == "function_call_output"}
    assert calls == outputs
    assert len(calls) < TURNS


def test_creates_a_topic_named_by_the_model(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)

    assert interviewer.switch_topic("Delivery") == "Switched to t2: Delivery"
    assert interviewer.active_topic_id == "t2"


def test_resolves_an_existing_topic_by_its_id(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)
    interviewer.switch_topic("Delivery")

    assert interviewer.switch_topic("t2") == "Switched to t2: Delivery"
    assert interviewer.active_topic_id == "t2"


def test_resolves_a_topic_name_regardless_of_case_and_spacing(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)
    interviewer.switch_topic("Delivery")
    interviewer.switch_topic("t1")

    assert interviewer.switch_topic("  delivery ") == "Switched to t2: Delivery"
    assert interviewer.active_topic_id == "t2"
    assert [topic.name for topic in interviewer.notebook.graph.topics] == ["Project overview", "Delivery"]


def test_rejects_switching_to_a_note(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)
    interviewer.switch_topic("Delivery")
    interviewer.notebook.add(["Deploy from the main branch."], "t2")

    with pytest.raises(ValueError, match="`n1` is not a topic"):
        interviewer.switch_topic("n1")

    assert interviewer.active_topic_id == "t2"


def test_rejects_switching_to_a_trashed_topic(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)
    interviewer.switch_topic("Delivery")
    interviewer.update_topic("t2", "trashed")

    with pytest.raises(ValueError, match="is trashed"):
        interviewer.switch_topic("t2")


def test_falls_back_to_the_overview_when_the_active_topic_is_trashed(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)
    interviewer.switch_topic("Delivery")

    assert interviewer.update_topic("t2", "trashed") == "Updated t2: Delivery (trashed)"
    assert interviewer.active_topic_id == "t1"


def test_stays_on_the_active_topic_when_another_one_is_trashed(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)
    interviewer.switch_topic("Delivery")
    interviewer.switch_topic("Pricing")

    assert interviewer.update_topic("t2", "trashed") == "Updated t2: Delivery (trashed)"
    assert interviewer.active_topic_id == "t3"


def test_keeps_a_topic_summary_when_only_its_status_changes(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)
    interviewer.update_topic("t1", "open", "Everything the project must do.")

    assert interviewer.update_topic("t1", "done") == "Updated t1: Project overview (done)"
    assert interviewer.notebook.initial_topic.summary == "Everything the project must do."


def test_rejects_trashing_the_overview_topic(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)

    with pytest.raises(ValueError, match="cannot be trashed"):
        interviewer.update_topic("t1", "trashed")

    assert interviewer.notebook.graph.topics[0].status == "open"


def test_captures_several_notes_under_the_active_topic(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)
    interviewer.switch_topic("Delivery")

    assert interviewer.capture_notes(["Ships weekly.", "Deploys on Fridays."]) == (
        "Added n1: Ships weekly.\nAdded n2: Deploys on Fridays."
    )
    assert [(note.id, note.topic_id) for note in interviewer.notebook.graph.notes] == [("n1", "t2"), ("n2", "t2")]


def test_reads_every_note_and_connection_without_a_query(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)
    interviewer.capture_notes(["Ships weekly.", "Runs on the web."])
    interviewer.connect_notes([CONNECTION])

    assert interviewer.read_notes() == (
        "- n1: Ships weekly.\n- n2: Runs on the web.\n\nConnections\n- n1 --constrains--> n2"
    )


def test_reads_an_empty_notebook_as_no_notes(tmp_path: Path) -> None:
    assert build_interviewer(tmp_path).read_notes() == "No notes found."


def test_edits_the_text_of_a_single_note(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)
    interviewer.capture_notes(["Ships weekly."])

    assert interviewer.edit_note("n1", "Ships daily.") == "Edited n1: Ships daily."
    assert interviewer.notebook.graph.notes[0].text == "Ships daily."


def test_deletes_every_requested_note(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)
    interviewer.capture_notes(["First.", "Second.", "Third."])

    assert interviewer.delete_notes(["n1", "n3"]) == "Deleted notes: n1, n3."
    assert [note.id for note in interviewer.notebook.graph.notes] == ["n2"]


def test_counts_only_the_connections_it_added(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)
    interviewer.capture_notes(["First.", "Second."])

    assert interviewer.connect_notes([CONNECTION]) == "Connected 1 relationship(s)."
    assert interviewer.connect_notes([CONNECTION]) == "Connected 0 relationship(s)."
    assert interviewer.notebook.graph.connections == [CONNECTION]


def test_counts_only_the_connections_it_removed(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)
    interviewer.capture_notes(["First.", "Second."])
    interviewer.connect_notes([CONNECTION])

    assert interviewer.disconnect_notes([CONNECTION]) == "Disconnected 1 relationship(s)."
    assert interviewer.disconnect_notes([CONNECTION]) == "Disconnected 0 relationship(s)."
    assert interviewer.notebook.graph.connections == []


def test_explores_reporting_only_what_follows_the_last_nested_tool_call(tmp_path: Path) -> None:
    client = FakeClient([
        [*partial_reply("Guessing before looking."), *response(call("c1", "search_web", query="cats"))],
        streamed_reply("Cats are mammals."),
    ])
    interviewer = build_interviewer(tmp_path, client)

    events = list(interviewer.explore("cats"))

    assert [event.call_id for event in events if isinstance(event, ToolCallStarted)] == ["c1"]
    assert events[-1] == ToolOutput("Cats are mammals.")


def test_explores_reporting_nothing_when_the_run_ends_on_a_tool_call(tmp_path: Path) -> None:
    client = FakeClient([
        [*partial_reply("Guessing before looking."), *response(call("c1", "search_web", query="cats"))],
        response(),
    ])
    interviewer = build_interviewer(tmp_path, client)

    assert list(interviewer.explore("cats"))[-1] == ToolOutput("")


def test_reports_a_failed_exploration_to_the_model(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path, FakeClient([failure("The provider is unavailable.")]))
    explore = next(tool for tool in interviewer.tools if tool.name == "explore")

    invocation = explore.invoke('{"query": "cats"}')
    list(invocation)

    assert invocation.failed
    assert invocation.output == "Tool call failed: The provider is unavailable."


def test_reports_an_unwritable_notebook_to_the_model(tmp_path: Path) -> None:
    interviewer = build_interviewer(tmp_path)
    interviewer.notebook.path.unlink()
    interviewer.notebook.path.mkdir()
    (interviewer.notebook.path / "blocker").write_text("taken")
    capture_notes = next(tool for tool in interviewer.tools if tool.name == "capture_notes")

    invocation = capture_notes.invoke('{"texts": ["A requirement"]}')
    list(invocation)

    assert invocation.failed
    assert cast("str", invocation.output).startswith("Tool call failed: Could not save the notebook file")
