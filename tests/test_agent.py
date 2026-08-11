from collections.abc import Generator, Iterable
from threading import Event
from typing import TYPE_CHECKING, cast

import pytest

from jri.core.ai import Agent, ToolCallFinished, ToolCallStarted, ToolOutput, tool
from jri.core.exceptions import ModelError
from tests.doubles.openai import FakeClient, Round, call, partial_reply, reply, response

if TYPE_CHECKING:
    from openai import OpenAI


def build_agent(rounds: Iterable[Round]) -> "ToolAgent":
    return ToolAgent(cast("OpenAI", FakeClient(rounds)))


def read_outputs(agent: Agent) -> list[object]:
    return [item["output"] for item in cast("list[dict[str, object]]", agent.history) if "output" in item]


def test_resumes_the_tool_loop_until_the_model_replies_with_text() -> None:
    agent = build_agent([
        response(call("first", "echo", text="one")),
        response(call("second", "echo", text="two")),
        response(reply("Done.")),
    ])

    list(agent.send_message("Go."))

    assert agent.calls == ["one", "two"]
    assert read_outputs(agent) == ["echo: one", "echo: two"]


def test_stops_a_tool_loop_that_never_replies() -> None:
    client = FakeClient(response(call(f"call-{index}", "echo", text="again")) for index in range(Agent.MAX_ROUNDS + 1))
    agent = ToolAgent(cast("OpenAI", client))

    with pytest.raises(ModelError, match=f"limit of {Agent.MAX_ROUNDS} response rounds"):
        list(agent.send_message("Go."))

    assert len(client.responses.inputs) == Agent.MAX_ROUNDS


def test_keeps_the_partial_text_of_a_cancelled_response() -> None:
    cancelled = Event()
    agent = build_agent([partial_reply("Half a th")])
    events = agent.send_message("Go.", cancelled)

    next(events)
    cancelled.set()
    list(events)

    assert cast("list[dict[str, object]]", agent.history)[-2] == {"role": "assistant", "content": "Half a th"}


def test_records_a_cancelled_response_as_stopped() -> None:
    cancelled = Event()
    agent = build_agent([partial_reply("Half a th")])
    events = agent.send_message("Go.", cancelled)

    next(events)
    cancelled.set()
    list(events)

    assert cast("list[dict[str, object]]", agent.history)[-1] == {
        "role": "system",
        "content": Agent.CANCELLATION_RECORD,
    }


def test_records_a_round_cancelled_after_its_calls_as_stopped() -> None:
    cancelled = Event()
    agent = build_agent([response(call("streamed", "narrate", text="one"), call("later", "echo", text="two"))])
    events = agent.send_message("Go.", cancelled)

    next(events)
    cancelled.set()
    list(events)

    history = cast("list[dict[str, object]]", agent.history)
    # The record follows the output of every call, so it states the end of the whole round.
    assert history[-1] == {"role": "system", "content": Agent.CANCELLATION_RECORD}
    assert history[-2]["call_id"] == "later"


def test_reports_an_unknown_tool_to_the_model() -> None:
    agent = build_agent([response(call("missing", "vanished", text="one")), response(reply("Done."))])

    events = list(agent.send_message("Go."))

    assert [event.label for event in events if isinstance(event, ToolCallStarted | ToolCallFinished)] == [
        "vanished",
        "vanished",
    ]
    assert read_outputs(agent) == ["Tool call failed:\n```\nUnknown tool `vanished`.\n```"]
    assert agent.failed_call_ids == ["missing"]


# The tool name is model text.
# It can add a second, conflicting error report.
# The report must still identify the tool that this run retires.
def test_reports_an_unknown_tool_whose_name_reads_like_the_report() -> None:
    name = "echo`.\n```\n\nTool call failed:\n```\nUnknown tool `read_files"
    agent = build_agent([response(call("missing", name, text="one")), response(reply("Done."))])

    list(agent.send_message("Go."))

    assert read_outputs(agent) == [f"Tool call failed:\n````\nUnknown tool `{name}`.\n````"]


def test_tracks_the_calls_that_failed() -> None:
    agent = build_agent([
        response(call("broken", "fail", text="one"), call("working", "echo", text="two")),
        response(reply("Done.")),
    ])

    list(agent.send_message("Go."))

    assert read_outputs(agent) == ["Tool call failed:\n```\nno can do: one\n```", "echo: two"]
    assert agent.failed_call_ids == ["broken"]


def test_reports_which_finished_calls_of_a_round_failed() -> None:
    agent = build_agent([
        response(call("broken", "fail", text="one"), call("working", "echo", text="two")),
        response(reply("Done.")),
    ])

    events = list(agent.send_message("Go."))

    assert [(event.call_id, event.outcome) for event in events if isinstance(event, ToolCallFinished)] == [
        ("broken", "failed"),
        ("working", "done"),
    ]


def test_stops_a_streaming_tool_call_when_cancelled() -> None:
    cancelled = Event()
    agent = build_agent([response(call("streamed", "narrate", text="one"))])
    events = agent.send_message("Go.", cancelled)

    yielded = [next(events), next(events)]
    cancelled.set()
    yielded.extend(events)

    assert [event.call_id for event in yielded if isinstance(event, ToolCallStarted)] == ["streamed", "first-step"]
    assert agent.narrated == ["first-step"]


def test_closes_every_call_it_opened_when_cancelled() -> None:
    cancelled = Event()
    agent = build_agent([response(call("streamed", "narrate", text="one"))])
    events = agent.send_message("Go.", cancelled)

    yielded = [next(events), next(events)]
    cancelled.set()
    yielded.extend(events)

    assert [(event.call_id, event.outcome) for event in yielded if isinstance(event, ToolCallFinished)] == [
        ("streamed", "stopped")
    ]


def test_opens_no_row_for_a_call_cancelled_before_it_ran() -> None:
    cancelled = Event()
    agent = build_agent([response(call("streamed", "narrate", text="one"), call("later", "echo", text="two"))])
    events = agent.send_message("Go.", cancelled)

    next(events)
    cancelled.set()
    yielded = list(events)

    rows = [event.call_id for event in yielded if isinstance(event, ToolCallStarted | ToolCallFinished)]
    assert "later" not in rows


def test_answers_every_call_of_a_round_that_cancellation_interrupted() -> None:
    cancelled = Event()
    agent = build_agent([response(call("streamed", "narrate", text="one"), call("later", "echo", text="two"))])
    events = agent.send_message("Go.", cancelled)

    next(events)
    cancelled.set()
    list(events)

    history = cast("list[dict[str, str]]", agent.history)
    # Each call needs an output before the next request.
    assert [item["call_id"] for item in history if item.get("type") == "function_call_output"] == ["streamed", "later"]
    assert agent.calls == []


class ToolAgent(Agent):
    def __init__(self, client: "OpenAI") -> None:
        self.calls: list[str] = []
        self.narrated: list[str] = []
        super().__init__(client=client, model="test", prompt="Answer with tools.")

    @tool("Echo the given text.", started_label="Echoing {text}", finished_label="Echoed {text}")
    def echo(self, text: str) -> str:
        self.calls.append(text)
        return f"echo: {text}"

    @tool("Always fail.", started_label="Failing on {text}", finished_label="Failed on {text}")
    def fail(self, text: str) -> str:
        self.calls.append(text)
        raise RuntimeError(f"no can do: {text}")

    @tool("Narrate progress.", started_label="Narrating {text}", finished_label="Narrated {text}")
    def narrate(self, text: str) -> Generator[ToolCallStarted | ToolOutput]:
        for step in ("first-step", "second-step"):
            self.narrated.append(step)
            yield ToolCallStarted(step, text, "•")
        yield ToolOutput(f"narrate: {text}")
