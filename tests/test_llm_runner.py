from typing import TYPE_CHECKING, cast

import pytest
from pydantic import BaseModel

from jri.core.ai import LLMRunner
from tests.doubles.openai import FakeClient, failed_response, incomplete_response, partial_reply, reply, response

if TYPE_CHECKING:
    from openai import OpenAI


def build_runner(parsed: object) -> LLMRunner:
    return LLMRunner(client=cast("OpenAI", FakeClient([], parsed=[parsed])), model="test")


def test_returns_the_parsed_output() -> None:
    assert build_runner(Output(answer="ready")).parse([], Output).answer == "ready"


def test_falls_back_to_the_aggregated_response_text() -> None:
    runner = build_runner(response(reply('{"answer": "aggregated"}')))

    assert runner.parse([], Output).answer == "aggregated"


def test_falls_back_to_the_streamed_text() -> None:
    runner = build_runner(partial_reply('{"answer": "streamed"}'))

    assert runner.parse([], Output).answer == "streamed"


def test_reports_why_a_response_was_cut_short() -> None:
    runner = build_runner(incomplete_response("max_output_tokens"))

    with pytest.raises(RuntimeError, match="incomplete: max_output_tokens"):
        runner.parse([], Output)


def test_reports_an_unexplained_cut_short_response() -> None:
    runner = build_runner(incomplete_response(None))

    with pytest.raises(RuntimeError, match="incomplete: unknown reason"):
        runner.parse([], Output)


def test_reports_a_failed_response() -> None:
    runner = build_runner(failed_response("the model overloaded"))

    with pytest.raises(RuntimeError, match="the model overloaded"):
        runner.parse([], Output)


def test_rejects_a_context_over_the_input_size_limit() -> None:
    runner = LLMRunner(client=cast("OpenAI", FakeClient([])), model="test", max_input_size=10)

    with pytest.raises(RuntimeError, match="over the 10 byte limit"):
        runner.parse([{"role": "user", "content": "far too many bytes"}], Output)


class Output(BaseModel):
    answer: str
