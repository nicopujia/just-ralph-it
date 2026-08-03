from typing import TYPE_CHECKING, cast

import pytest
from pydantic import BaseModel

from jri.core.ai import LLMRunner
from tests.doubles.openai import FakeClient, failed_response, incomplete_response, streamed_reply

if TYPE_CHECKING:
    from openai import OpenAI


class Output(BaseModel):
    answer: str


def build_runner(parsed: object) -> LLMRunner:
    return LLMRunner(client=cast("OpenAI", FakeClient([], parsed=[parsed])), model="test")


def test_parse_returns_the_parsed_output() -> None:
    assert build_runner(Output(answer="ready")).parse([], Output).answer == "ready"


def test_parse_falls_back_to_the_streamed_text() -> None:
    runner = build_runner(streamed_reply('{"answer": "streamed"}'))

    assert runner.parse([], Output).answer == "streamed"


def test_parse_reports_why_the_response_was_cut_short() -> None:
    runner = build_runner(incomplete_response("max_output_tokens"))

    with pytest.raises(RuntimeError, match="incomplete: max_output_tokens"):
        runner.parse([], Output)


def test_parse_reports_an_unexplained_cut_short_response() -> None:
    runner = build_runner(incomplete_response(None))

    with pytest.raises(RuntimeError, match="incomplete: unknown reason"):
        runner.parse([], Output)


def test_parse_reports_a_failed_response() -> None:
    runner = build_runner(failed_response("the model overloaded"))

    with pytest.raises(RuntimeError, match="the model overloaded"):
        runner.parse([], Output)
