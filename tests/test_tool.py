from collections.abc import Generator
from typing import TYPE_CHECKING, cast

from jri.core.ai import Invocation, Tool, ToolCallStarted, ToolOutput, tool

if TYPE_CHECKING:
    from openai.types.responses import ResponseFunctionCallOutputItemListParam


KILOBYTE = 1024
TRUNCATION_NOTICE = "[Output truncated. Try splitting into more targeted calls.]"


def build_tools(owner: object) -> dict[str, Tool]:
    return {discovered.name: discovered for discovered in Tool.discover(owner)}


def build_tool(name: str) -> Tool:
    return build_tools(Toolbox())[name]


# The two sizes below are the budget itself, stated by a caller
# instead of read back off the constant: an expectation computed from
# `MAX_OUTPUT_LENGTH` holds for every value that constant could take,
# which is how a budget too small for a screenshot went unnoticed.
def test_keeps_a_long_source_file_whole() -> None:
    body = "x" * (90 * KILOBYTE)

    invocation = Invocation(body)
    list(invocation)

    assert invocation.output == body


def test_cuts_a_directory_listing_of_a_whole_monorepo() -> None:
    invocation = Invocation("x" * (300 * KILOBYTE))
    list(invocation)

    output = cast("str", invocation.output)

    assert output == "x" * 100_000 + f"\n\n{TRUNCATION_NOTICE}"


def test_keeps_output_of_exactly_the_maximum_length() -> None:
    invocation = Invocation("x" * Invocation.MAX_OUTPUT_LENGTH)
    list(invocation)

    assert invocation.output == "x" * Invocation.MAX_OUTPUT_LENGTH


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
