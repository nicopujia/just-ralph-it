from typing import TYPE_CHECKING, cast

from jri.core.ai import Invocation, Tool, tool

if TYPE_CHECKING:
    from openai.types.responses import ResponseFunctionCallOutputItemListParam


TRUNCATION_NOTICE = "[Output truncated. Try splitting into more targeted calls.]"


def build_tool() -> Tool:
    return Tool.discover(Toolbox())[0]


def test_truncates_long_text_output() -> None:
    invocation = Invocation("x" * (Invocation.MAX_OUTPUT_LENGTH + 1))
    list(invocation)

    output = cast("str", invocation.output)

    assert output == "x" * Invocation.MAX_OUTPUT_LENGTH + f"\n\n{TRUNCATION_NOTICE}"


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


def test_replaces_an_oversized_attachment_with_the_truncation_notice() -> None:
    output = cast(
        "ResponseFunctionCallOutputItemListParam",
        [{"type": "input_image", "image_url": "x" * (Invocation.MAX_OUTPUT_LENGTH + 1)}],
    )

    invocation = Invocation(output)
    list(invocation)
    result = cast("ResponseFunctionCallOutputItemListParam", invocation.output)

    assert result == [{"type": "input_text", "text": TRUNCATION_NOTICE}]


def test_reports_invalid_arguments_to_the_model() -> None:
    invocation = build_tool().invoke('{"text": 7}')
    list(invocation)

    assert invocation.failed
    assert cast("str", invocation.output).startswith("Tool call failed:")


def test_labels_a_call_by_its_tool_name_when_the_arguments_are_invalid() -> None:
    discovered = build_tool()

    assert discovered.format_label(discovered.started_label, '{"text": 7}') == "echo"
    assert discovered.format_label(discovered.started_label, '{"text": "one"}') == "Echoing one"


class Toolbox:
    """Owner of a single tool to discover."""

    PREFIX = "echo: "

    @tool("Echo the given text.", started_label="Echoing {text}", finished_label="Echoed {text}")
    def echo(self, text: str) -> str:
        return f"{self.PREFIX}{text}"
