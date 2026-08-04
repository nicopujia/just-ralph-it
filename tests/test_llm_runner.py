from typing import TYPE_CHECKING, cast

import pytest
from openai import omit
from pydantic import BaseModel

from jri.core.ai import LLMRunner, ReasoningDelta
from jri.core.exceptions import ModelError
from tests.doubles.openai import (
    FakeClient,
    failed_response,
    incomplete_response,
    partial_reply,
    reasoning,
    reply,
    response,
)

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


@pytest.mark.parametrize(
    "event_type", ["response.reasoning.delta", "response.reasoning_text.delta", "response.reasoning_summary_text.delta"]
)
def test_streams_the_reasoning_of_the_model(event_type: str) -> None:
    client = FakeClient([reasoning("weighing the options", event_type)])
    runner = LLMRunner(client=cast("OpenAI", client), model="test")

    assert list(runner.respond([]).events) == [ReasoningDelta("weighing the options")]


def test_reports_a_response_without_any_output() -> None:
    runner = build_runner(response())

    with pytest.raises(ModelError, match="did not contain a parsed output"):
        runner.parse([], Output)


def test_reports_a_response_that_is_not_valid_json() -> None:
    runner = build_runner(response(reply("Sure! Here is the answer you asked for.")))

    with pytest.raises(ModelError):
        runner.parse([], Output)


def test_reports_why_a_response_was_cut_short() -> None:
    runner = build_runner(incomplete_response("max_output_tokens"))

    with pytest.raises(ModelError, match="incomplete: max_output_tokens"):
        runner.parse([], Output)


def test_reports_an_unexplained_cut_short_response() -> None:
    runner = build_runner(incomplete_response(None))

    with pytest.raises(ModelError, match="incomplete: unknown reason"):
        runner.parse([], Output)


def test_reports_a_failed_response() -> None:
    runner = build_runner(failed_response("the model overloaded"))

    with pytest.raises(ModelError, match="the model overloaded"):
        runner.parse([], Output)


@pytest.mark.parametrize("temperature", [0, None], ids=["configured", "omitted"])
def test_sends_temperature_only_when_configured(temperature: float | None) -> None:
    client = FakeClient([], parsed=[Output(answer="ready")])

    LLMRunner(client=cast("OpenAI", client), model="test", temperature=temperature).parse([], Output)

    assert client.responses.options[-1]["temperature"] == (omit if temperature is None else temperature)


def test_rejects_a_context_over_the_input_size_limit() -> None:
    runner = LLMRunner(client=cast("OpenAI", FakeClient([])), model="test", max_input_size=10)

    with pytest.raises(ModelError, match="over the 10 byte limit"):
        runner.parse([{"role": "user", "content": "far too many bytes"}], Output)


class Output(BaseModel):
    answer: str
