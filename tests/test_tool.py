from typing import TYPE_CHECKING, cast

from jri.core.agents.explorer import Explorer
from jri.core.agents.shared import MAX_OUTPUT_LENGTH
from jri.core.agents.shared.tool import Invocation

if TYPE_CHECKING:
    from pathlib import Path

    from openai.types.responses import ResponseFunctionCallOutputItemListParam


def test_structured_tool_output_is_truncated() -> None:
    output = cast(
        "ResponseFunctionCallOutputItemListParam",
        [
            {"type": "input_text", "text": "first"},
            {"type": "input_text", "text": "x" * MAX_OUTPUT_LENGTH},
            {"type": "input_text", "text": "omitted"},
        ],
    )

    invocation = Invocation(output)
    list(invocation)
    result = cast("ResponseFunctionCallOutputItemListParam", invocation.output)

    assert result[0] == output[0]
    assert result[1]["text"].startswith("x" * (MAX_OUTPUT_LENGTH - len("first")))
    assert result[1]["text"].endswith("[Output truncated. Try splitting into more targeted calls.]")
    assert len(result) == len(output) - 1


def test_read_files_selects_lines(tmp_path: "Path") -> None:
    path = tmp_path / "example.txt"
    path.write_text("one\ntwo\nthree\nfour\n")

    result = Explorer.read_files([str(path)], start_line=2, end_line=3)

    assert result[1] == {"type": "input_text", "text": "two\nthree\n"}
