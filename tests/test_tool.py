import logging
from collections.abc import Generator
from typing import TYPE_CHECKING, Annotated, cast

import pytest
from pydantic import PlainSerializer

from jri.core.ai import Invocation, Tool, ToolCallStarted, ToolOutput, tool
from jri.core.exceptions import ReplayError
from jri.lib import prompt

if TYPE_CHECKING:
    from openai.types.responses import ResponseFunctionCallOutputItemListParam


TRUNCATION_NOTICE = "[Output truncated. Try splitting into more targeted calls.]"
# A payload holding a run of backticks is quoted inside a longer fence,
# so what the cut has to leave room for is not three characters.
QUOTED_RUN = "`" * 40
QUOTING_FENCE = "`" * 41


def build_tools(owner: object) -> dict[str, Tool]:
    return {discovered.name: discovered for discovered in Tool.discover(owner)}


def build_tool(name: str) -> Tool:
    return build_tools(Toolbox())[name]


# A label whose wording reaches for something that can refuse it, the
# way naming a file reaches for a filesystem that may not answer.
def fail_to_describe(text: str) -> str:
    raise RuntimeError(f"Could not describe {text}.")


# The three sizes below are the budget itself, stated by a caller
# instead of read back off the constant: an expectation computed from
# `MAX_OUTPUT_LENGTH` holds for every value that constant could take,
# which is how a budget too small for a screenshot went unnoticed. They
# are plain character counts, because that is what the budget counts:
# a unit of 1024 would read them onto a scale it never used.
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


# A model is told that nothing inside a block is JRI talking to it, so
# a notice cut into one says nothing, and a block left open takes
# whatever follows it as more of the text it quotes.
def test_ends_the_block_a_cut_output_leaves_open() -> None:
    quoted = prompt.render(content=f"{QUOTED_RUN}\n" + "x" * Invocation.MAX_OUTPUT_LENGTH)

    invocation = Invocation(quoted)
    list(invocation)
    output = cast("str", invocation.output)

    assert output.startswith(f"Content:\n{QUOTING_FENCE}\n")
    assert output.endswith(f"\n{QUOTING_FENCE}\n\n{TRUNCATION_NOTICE}")
    assert len(output) == Invocation.MAX_OUTPUT_LENGTH + len(f"\n\n{TRUNCATION_NOTICE}")


def test_ends_the_block_a_cut_structured_output_leaves_open() -> None:
    quoted = prompt.render(content=f"{QUOTED_RUN}\n" + "x" * Invocation.MAX_OUTPUT_LENGTH)
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
    assert truncated.startswith(f"Content:\n{QUOTING_FENCE}\n")
    assert truncated.endswith(f"\n{QUOTING_FENCE}\n\n{TRUNCATION_NOTICE}")


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
    assert cast("str", invocation.output).startswith("Tool call failed:\n```\n")


def test_labels_a_call_by_its_tool_name_when_the_arguments_are_invalid() -> None:
    discovered = build_tool("echo")

    assert discovered.format_label(discovered.started_label, '{"text": 7}') == "echo"
    assert discovered.format_label(discovered.started_label, '{"text": "one"}') == "Echoing one"


# A row is decoration, so one nothing can word costs its own wording
# and nothing else: the call runs, and the log says the label failed,
# so a label no argument could ever word is still findable.
def test_keeps_a_call_whose_label_cannot_be_worded(caplog: pytest.LogCaptureFixture) -> None:
    discovered = build_tool("describe")

    with caplog.at_level(logging.INFO, logger="jri"):
        label = discovered.format_label(discovered.started_label, '{"text": "one"}')
        invocation = discovered.invoke('{"text": "one"}')
        list(invocation)

    assert label == "describe"
    assert invocation.outcome == "done"
    assert invocation.output == "described: one"
    assert any(record.message.startswith("label_failed") for record in caplog.records)


def test_keeps_the_output_of_a_stream_that_fails_after_reporting_it() -> None:
    invocation = build_tool("give_up").invoke('{"text": "one"}')

    list(invocation)

    assert invocation.outcome == "failed"
    assert invocation.output == "partial: one\n\nTool call failed:\n```\nno more: one\n```"


def test_keeps_the_structured_output_of_a_stream_that_fails_after_reporting_it() -> None:
    invocation = build_tool("give_up_after_listing").invoke('{"text": "one"}')

    list(invocation)

    assert invocation.outcome == "failed"
    assert invocation.output == [
        {"type": "input_text", "text": "partial: one"},
        {"type": "input_text", "text": "Tool call failed:\n```\nno more: one\n```"},
    ]


def test_reports_the_reason_a_call_failed() -> None:
    invocation = build_tool("give_up_loudly").invoke('{"text": "x"}')

    list(invocation)

    # The reason comes from the exception, so the fence the output is
    # rendered in never reaches it.
    assert invocation.detail == "x" * Invocation.MAX_DETAIL_LENGTH
    assert cast("str", invocation.output).startswith("partial: x\n\nTool call failed:")


def test_reports_an_output_that_says_nothing_as_empty() -> None:
    invocation = build_tool("find_nothing").invoke('{"text": "one"}')

    list(invocation)

    assert invocation.outcome == "empty"
    assert invocation.output == "nothing found: one"


def test_reports_a_stream_that_never_produced_an_output() -> None:
    invocation = build_tool("narrate").invoke('{"text": "one"}')

    list(invocation)

    assert invocation.outcome == "failed"
    assert cast("str", invocation.output).startswith("Tool call failed:")


def test_marks_a_stream_abandoned_before_its_output_as_failed() -> None:
    invocation = build_tool("narrate").invoke('{"text": "one"}')

    next(iter(invocation))

    # A call reported to the model as failed
    # must not be replayed on rewind.
    assert cast("str", invocation.output).startswith("Tool call failed:")
    assert invocation.outcome == "failed"


def test_skips_a_tool_that_is_not_replayed() -> None:
    toolbox = Toolbox()
    tools = build_tools(toolbox)

    tools["peek"].replay('{"text": "one"}')
    tools["record"].replay('{"text": "two"}')

    assert toolbox.recorded == ["two"]


# A replay has no model to hand a rendered failure to, so the caller
# rebuilding from it is the only one who can be told.
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
