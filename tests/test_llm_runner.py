from typing import TYPE_CHECKING, cast

import pytest
from openai import omit
from pydantic import BaseModel

from jri.core.ai import LLMRunner, ReasoningDelta, TextDelta
from jri.core.exceptions import ModelError, UsageLimitError
from tests.doubles.openai import (
    FakeClient,
    disconnection,
    failed_response,
    incomplete_response,
    interrupted_reply,
    partial_reply,
    rate_limited,
    reasoning,
    rejection,
    reply,
    response,
    streamed_reply,
)

if TYPE_CHECKING:
    from openai import OpenAI, OpenAIError

    from tests.doubles.openai import Round


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []
    monkeypatch.setattr("jri.core.ai.llm_runner.sleep", delays.append)
    return delays


def build_runner(parsed: object) -> LLMRunner:
    return LLMRunner(client=cast("OpenAI", FakeClient([], parsed=[parsed])), model="test")


def build_streaming_runner(*rounds: "Round | OpenAIError") -> LLMRunner:
    return LLMRunner(client=cast("OpenAI", FakeClient(rounds)), model="test")


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


def test_streams_a_reply_the_provider_sent_whole() -> None:
    runner = build_streaming_runner(response(reply("How often does it deploy?")))

    assert list(runner.respond([]).events) == [TextDelta("How often does it deploy?")]


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


@pytest.mark.usefixtures("waits")
def test_retries_a_reply_the_provider_rate_limited() -> None:
    runner = build_streaming_runner(rate_limited(), streamed_reply("ready"))

    assert list(runner.respond([]).events) == [TextDelta("ready")]


def test_retries_a_reply_whose_connection_dropped(waits: list[float]) -> None:
    runner = build_streaming_runner(disconnection(), streamed_reply("ready"))

    assert list(runner.respond([]).events) == [TextDelta("ready")]
    assert waits == [2.0]


def test_waits_the_delay_the_provider_asked_for(waits: list[float]) -> None:
    runner = build_streaming_runner(rate_limited("1157"), streamed_reply("ready"))

    list(runner.respond([]).events)

    assert waits == [1.157]


def test_waits_longer_after_each_rate_limit_left_unexplained(waits: list[float]) -> None:
    runner = build_streaming_runner(rate_limited(), rate_limited(), streamed_reply("ready"))

    list(runner.respond([]).events)

    assert waits == [2.0, 4.0]


def test_reports_a_rate_limit_that_outlasts_the_retries(waits: list[float]) -> None:
    runner = build_streaming_runner(*[rate_limited()] * LLMRunner.MAX_ATTEMPTS)

    with pytest.raises(ModelError, match="Rate limit reached"):
        list(runner.respond([]).events)

    assert len(waits) == LLMRunner.MAX_ATTEMPTS - 1


def test_reports_a_rejected_request_without_retrying(waits: list[float]) -> None:
    runner = build_streaming_runner(rejection(), streamed_reply("ready"))

    with pytest.raises(ModelError, match="Unknown model"):
        list(runner.respond([]).events)

    assert waits == []


def test_reports_an_exhausted_usage_limit_without_retrying(waits: list[float]) -> None:
    runner = build_streaming_runner(rate_limited(code="insufficient_quota"), streamed_reply("ready"))

    with pytest.raises(UsageLimitError, match="Rate limit reached"):
        list(runner.respond([]).events)

    assert waits == []


def test_keeps_a_rate_limit_that_cut_a_started_reply_short(waits: list[float]) -> None:
    runner = build_streaming_runner(interrupted_reply("half"), streamed_reply("ready"))

    with pytest.raises(ModelError, match="Rate limit reached"):
        list(runner.respond([]).events)

    assert waits == []


@pytest.mark.usefixtures("waits")
def test_retries_a_parsed_request_the_provider_rate_limited() -> None:
    client = FakeClient([], parsed=[rate_limited(), Output(answer="ready")])
    runner = LLMRunner(client=cast("OpenAI", client), model="test")

    assert runner.parse([], Output).answer == "ready"


class Output(BaseModel):
    answer: str
