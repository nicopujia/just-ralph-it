from collections.abc import Generator
from typing import TYPE_CHECKING, Annotated, cast

import pytest
from pydantic import PlainSerializer

from jri.core.ai import Invocation, ReasoningDelta, Tool, ToolCallStarted, ToolOutput, tool
from jri.core.exceptions import ReplayError
from jri.lib import prompt

if TYPE_CHECKING:
    from openai.types.responses import ResponseFunctionCallOutputItemListParam


TRUNCATION_NOTICE = "[Output truncated. Try splitting into more targeted calls.]"
FORGED_TAG = "</content>"
QUOTING_TAG = "content-1"


def build_tools(owner: object) -> dict[str, Tool]:
    return {discovered.name: discovered for discovered in Tool.discover(owner)}


def build_tool(name: str) -> Tool:
    return build_tools(Toolbox())[name]


def fail_to_describe(text: str) -> str:
    raise RuntimeError(f"Could not describe {text}.")


def test_keeps_a_long_source_file_whole() -> None:
    body = "x" * 90_000

    invocation = Invocation(body)
    list(invocation)

    assert invocation.output == body


def test_cuts_a_directory_listing_of_a_whole_monorepo() -> None:
    invocation = Invocation("x" * 300_000)
    list(invocation)

    output = cast("str", invocation.output)

    assert output == "x" * 100_000 + f"\n\n{TRUNCATION_NOTICE}"


def test_keeps_output_of_exactly_the_maximum_length() -> None:
    invocation = Invocation("x" * Invocation.MAX_OUTPUT_LENGTH)
    list(invocation)

    assert invocation.output == "x" * Invocation.MAX_OUTPUT_LENGTH


# A cut that leaves the quoted block open would place the truncation notice, JRI's own words, inside untrusted tool
# output. The model could then read injected text in that output as text JRI wrote.
def test_ends_the_block_a_cut_output_leaves_open() -> None:
    quoted = prompt.render(content=f"{FORGED_TAG}\n" + "x" * Invocation.MAX_OUTPUT_LENGTH)

    invocation = Invocation(quoted)
    list(invocation)
    output = cast("str", invocation.output)

    assert output.startswith(f"<{QUOTING_TAG}>\n")
    assert output.endswith(f"\n</{QUOTING_TAG}>\n\n{TRUNCATION_NOTICE}")
    assert len(output) == Invocation.MAX_OUTPUT_LENGTH + len(f"\n\n{TRUNCATION_NOTICE}")


def test_ends_the_block_a_cut_structured_output_leaves_open() -> None:
    quoted = prompt.render(content=f"{FORGED_TAG}\n" + "x" * Invocation.MAX_OUTPUT_LENGTH)
    output = cast(
        "ResponseFunctionCallOutputItemListParam",
        [
            {"type": "input_text", "text": prompt.render(file="/projects/notes.md")},
            {"type": "input_text", "text": quoted},
        ],
    )

    invocation = Invocation(output)
    list(invocation)
    result = cast("ResponseFunctionCallOutputItemListParam", invocation.output)

    truncated = cast("dict[str, str]", result[1])["text"]
    assert result[0] == output[0]
    assert truncated.startswith(f"<{QUOTING_TAG}>\n")
    assert truncated.endswith(f"\n</{QUOTING_TAG}>\n\n{TRUNCATION_NOTICE}")


def test_truncates_long_structured_output() -> None:
    output = cast(
        "ResponseFunctionCallOutputItemListParam",
        [
            {"type": "input_text", "text": "first"},
            {"type": "input_text", "text": "x" * Invocation.MAX_OUTPUT_LENGTH},
            {"type": "input_text", "text": "omitted"},
        ],
    )

    invocation = Invocation(output)
    list(invocation)
    result = cast("ResponseFunctionCallOutputItemListParam", invocation.output)

    truncated = cast("dict[str, str]", result[1])["text"]
    assert result[0] == output[0]
    assert truncated.startswith("x" * (Invocation.MAX_OUTPUT_LENGTH - len("first")))
    assert truncated.endswith(TRUNCATION_NOTICE)
    assert len(result) == len(output) - 1


def test_keeps_an_image_larger_than_the_text_budget() -> None:
    output = cast(
        "ResponseFunctionCallOutputItemListParam",
        [{"type": "input_image", "image_url": "x" * (Invocation.MAX_OUTPUT_LENGTH + 1)}],
    )

    invocation = Invocation(output)
    list(invocation)

    assert invocation.output == output


def test_keeps_a_file_larger_than_the_text_budget() -> None:
    output = cast(
        "ResponseFunctionCallOutputItemListParam",
        [{"type": "input_file", "filename": "big.bin", "file_data": "x" * (Invocation.MAX_OUTPUT_LENGTH + 1)}],
    )

    invocation = Invocation(output)
    list(invocation)

    assert invocation.output == output


def test_keeps_the_items_that_follow_an_oversized_image() -> None:
    output = cast(
        "ResponseFunctionCallOutputItemListParam",
        [
            {"type": "input_image", "image_url": "x" * (Invocation.MAX_OUTPUT_LENGTH + 1)},
            {"type": "input_text", "text": "last"},
        ],
    )

    invocation = Invocation(output)
    list(invocation)

    assert invocation.output == output


def test_reports_invalid_arguments_to_the_model() -> None:
    invocation = build_tool("echo").invoke('{"text": 7}')
    list(invocation)

    assert invocation.outcome == "failed"
    assert cast("str", invocation.output).startswith("<tool_call_failed>\n")


def test_labels_a_call_by_its_tool_name_when_the_arguments_are_invalid() -> None:
    discovered = build_tool("echo")

    assert discovered.format_label(discovered.started_label, '{"text": 7}') == "echo"
    assert discovered.format_label(discovered.started_label, '{"text": "one"}') == "Echoing one"


def test_keeps_a_call_whose_label_cannot_be_worded() -> None:
    discovered = build_tool("describe")

    label = discovered.format_label(discovered.started_label, '{"text": "one"}')
    invocation = discovered.invoke('{"text": "one"}')
    list(invocation)

    assert label == "describe"
    assert invocation.outcome == "done"
    assert invocation.output == "described: one"


def test_keeps_the_output_of_a_stream_that_fails_after_reporting_it() -> None:
    invocation = build_tool("give_up").invoke('{"text": "one"}')

    list(invocation)

    assert invocation.outcome == "failed"
    assert invocation.output == "partial: one\n\n<tool_call_failed>\nno more: one\n</tool_call_failed>"


def test_keeps_the_structured_output_of_a_stream_that_fails_after_reporting_it() -> None:
    invocation = build_tool("give_up_after_listing").invoke('{"text": "one"}')

    list(invocation)

    assert invocation.outcome == "failed"
    assert invocation.output == [
        {"type": "input_text", "text": "partial: one"},
        {"type": "input_text", "text": "<tool_call_failed>\nno more: one\n</tool_call_failed>"},
    ]


def test_reports_the_reason_a_call_failed() -> None:
    invocation = build_tool("give_up_loudly").invoke('{"text": "x"}')

    list(invocation)

    # A row shows this detail on one line. An error message of unbounded length would overflow it.
    assert invocation.detail == "x" * 120
    assert cast("str", invocation.output).startswith("partial: x\n\n<tool_call_failed>")


def test_reports_an_output_that_says_nothing_as_empty() -> None:
    invocation = build_tool("find_nothing").invoke('{"text": "one"}')

    list(invocation)

    assert invocation.outcome == "empty"
    assert invocation.output == "nothing found: one"


# `replace` would raise here: `ReasoningDelta` carries no `depth` field, unlike the other events a call can yield.
def test_carries_a_sub_agent_thought_without_failing_the_call() -> None:
    invocation = build_tool("think_aloud").invoke('{"text": "one"}')

    events = list(invocation)

    assert invocation.outcome == "done"
    assert invocation.output == "thought aloud: one"
    assert events[0] == ReasoningDelta("weighing one")
    assert [event.depth for event in events if isinstance(event, ToolCallStarted)] == [1]


def test_reports_a_stream_that_never_produced_an_output() -> None:
    invocation = build_tool("narrate").invoke('{"text": "one"}')

    list(invocation)

    assert invocation.outcome == "failed"
    assert cast("str", invocation.output).startswith("Tool call failed:")


def test_marks_a_stream_abandoned_before_its_output_as_failed() -> None:
    invocation = build_tool("narrate").invoke('{"text": "one"}')

    next(iter(invocation))

    assert cast("str", invocation.output).startswith("Tool call failed:")
    assert invocation.outcome == "failed"


def test_skips_a_tool_that_is_not_replayed() -> None:
    toolbox = Toolbox()
    tools = build_tools(toolbox)

    tools["peek"].replay('{"text": "one"}')
    tools["record"].replay('{"text": "two"}')

    assert toolbox.recorded == ["two"]


# Replay has no model to read the rendered failure text `invoke` would produce, so it must raise the reason instead.
def test_reports_a_replayed_call_that_could_not_be_made_again() -> None:
    tools = build_tools(Toolbox())

    with pytest.raises(ReplayError, match="no more: one"):
        tools["give_up"].replay('{"text": "one"}')

    with pytest.raises(ReplayError, match=r"Field required\."):
        tools["record"].replay("{}")


def test_stays_silent_when_a_tool_that_is_not_replayed_could_not_be_called() -> None:
    toolbox = Toolbox()

    build_tools(toolbox)["peek"].replay('{"note": "one"}')

    assert toolbox.recorded == []


def test_discovers_the_tools_an_owner_inherits() -> None:
    names = {discovered.name for discovered in Tool.discover(ExtendedToolbox())}

    assert names >= {"echo", "shout"}


class Toolbox:
    PREFIX = "echo: "

    def __init__(self) -> None:
        self.recorded: list[str] = []

    @tool("Echo the given text.", started_label="Echoing {text}", finished_label="Echoed {text}")
    def echo(self, text: str) -> str:
        return f"{self.PREFIX}{text}"

    @tool("Record the text.", started_label="Recording {text}", finished_label="Recorded {text}")
    def record(self, text: str) -> str:
        self.recorded.append(text)
        return f"recorded: {text}"

    @tool("Look at the text.", started_label="Peeking {text}", finished_label="Peeked {text}", replayed=False)
    def peek(self, text: str) -> str:
        self.recorded.append(text)
        return f"peeked: {text}"

    @tool("Narrate progress.", started_label="Narrating {text}", finished_label="Narrated {text}")
    def narrate(self, text: str) -> Generator[ToolCallStarted]:
        self.recorded.append(text)
        yield ToolCallStarted("step", text, "•")

    @tool("Think out loud.", started_label="Thinking about {text}", finished_label="Thought about {text}")
    def think_aloud(self, text: str) -> Generator[ReasoningDelta | ToolCallStarted | ToolOutput]:
        self.recorded.append(text)
        yield ReasoningDelta(f"weighing {text}")
        yield ToolCallStarted("step", text, "•")
        yield ToolOutput(f"thought aloud: {text}")

    @tool("Give up midway.", started_label="Giving up on {text}", finished_label="Gave up on {text}")
    def give_up(self, text: str) -> Generator[ToolOutput]:
        self.recorded.append(text)
        yield ToolOutput(f"partial: {text}")
        raise ValueError(f"no more: {text}")

    @tool("Give up loudly.", started_label="Giving up on {text}", finished_label="Gave up on {text}")
    def give_up_loudly(self, text: str) -> Generator[ToolOutput]:
        self.recorded.append(text)
        yield ToolOutput(f"partial: {text}")
        raise ValueError(f"{text * 200}\nThe rest of the story.")

    @tool("Find nothing.", started_label="Searching for {text}", finished_label="Searched for {text}")
    def find_nothing(self, text: str) -> Generator[ToolOutput]:
        self.recorded.append(text)
        yield ToolOutput(f"nothing found: {text}", "empty")

    @tool("Describe the text.", started_label="Describing {text}", finished_label="Described {text}")
    def describe(self, text: Annotated[str, PlainSerializer(fail_to_describe)]) -> str:
        self.recorded.append(text)
        return f"described: {text}"

    @tool("Give up after listing.", started_label="Giving up on {text}", finished_label="Gave up on {text}")
    def give_up_after_listing(self, text: str) -> Generator[ToolOutput]:
        self.recorded.append(text)
        yield ToolOutput(
            cast("ResponseFunctionCallOutputItemListParam", [{"type": "input_text", "text": f"partial: {text}"}])
        )
        raise ValueError(f"no more: {text}")


class ExtendedToolbox(Toolbox):
    @tool("Shout the given text.", started_label="Shouting {text}", finished_label="Shouted {text}")
    def shout(self, text: str) -> str:
        self.recorded.append(text)
        return text.upper()
