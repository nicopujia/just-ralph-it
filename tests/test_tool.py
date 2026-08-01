from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from jri.core.ai import MAX_OUTPUT_LENGTH, Explorer, Invocation
from tests.doubles.openai import FakeClient

if TYPE_CHECKING:
    from pathlib import Path

    from openai.types.responses import ResponseFunctionCallOutputItemListParam

    from jri.core.settings import Settings


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

    settings = SimpleNamespace(
        cwd=tmp_path,
        llm=SimpleNamespace(client=FakeClient([])),
        agents=SimpleNamespace(explorer=SimpleNamespace(model="test", temperature=0, reasoning_effort=None)),
    )
    result = Explorer(cast("Settings", settings)).read_files([path.name], start_line=2, end_line=3)

    assert result[1] == {"type": "input_text", "text": "two\nthree\n"}
