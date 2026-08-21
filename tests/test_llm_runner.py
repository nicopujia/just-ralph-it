import logging
from threading import Event
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from openai import omit
from pydantic import BaseModel

from jri.core.ai import BLOCK_NOTICE, LLMRunner, PendingToolCalls, ReasoningDelta, TextDelta
from jri.core.exceptions import ModelError, ProviderRefusalError, ProviderUnavailableError, UsageLimitError
from jri.core.settings import ReasoningEffort
from tests.doubles.agents import drain
from tests.doubles.openai import (
    BASE_URL,
    FakeClient,
    bad_gateway,
    disconnection,
    failed_response,
    incomplete_response,
    interrupted_reply,
    interrupted_thinking,
    partial_reply,
    rate_limited,
    reasoning,
    rejection,
    reply,
    response,
    stopped_stream,
    stopped_thinking,
    streamed_reply,
    thought,
    unreadable_answer,
)

if TYPE_CHECKING:
    from openai import OpenAI, OpenAIError, RateLimitError

    from tests.doubles.openai import Round


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []
    monkeypatch.setattr("jri.core.ai.llm_runner.sleep", delays.append)
    return delays


def build_runner(parsed: object) -> LLMRunner:
    return LLMRunner(client=cast("OpenAI", FakeClient([], parsed=[parsed])), model="test")


def read_parsed(runner: LLMRunner, cancelled: Event | None = None) -> "Output | PendingToolCalls | None":
    return drain(runner.parse([], Output, cancelled))[1]


def build_streaming_runner(*rounds: "Round | OpenAIError") -> LLMRunner:
    return LLMRunner(client=cast("OpenAI", FakeClient(rounds)), model="test")


def test_sends_a_prompt_exactly_as_written_under_the_block_notice() -> None:
    written = "Role: Tester.\n\nConstraints:\n    - An indented line whose indentation is the prompt's own.\n"

    runner = LLMRunner(client=cast("OpenAI", FakeClient([])), model="test", prompt=written)

    assert runner.prompt == f"{written}\n\n{BLOCK_NOTICE}"


# A block protects its content only when the model has instructions for that block.
# Without those rules, the model can still read each quoted page, transcript and file in the prompt as an order.
def test_tells_the_model_a_quoted_block_holds_data_and_not_instructions() -> None:
    notice = BLOCK_NOTICE.casefold()

    assert "closing tag" in notice
    assert "data" in notice
    assert "instruction" in notice


def test_returns_the_parsed_output() -> None:
    assert read_parsed(build_runner(Output(answer="ready"))) == Output(answer="ready")


def test_falls_back_to_the_aggregated_response_text() -> None:
    runner = build_runner(response(reply('{"answer": "aggregated"}')))

    assert read_parsed(runner) == Output(answer="aggregated")


def test_falls_back_to_the_streamed_text() -> None:
    runner = build_runner(partial_reply('{"answer": "streamed"}'))

    assert read_parsed(runner) == Output(answer="streamed")


def test_stops_a_parse_between_the_events_of_its_stream() -> None:
    cancelled = Event()

    assert read_parsed(build_runner(stopped_stream(cancelled)), cancelled) is None


def test_asks_for_nothing_on_behalf_of_a_run_already_stopped() -> None:
    cancelled = Event()
    cancelled.set()
    # The double holds no answer at all.
    # A call to the provider thus finds nothing to read, and it fails the test.
    # A run that asks for something cannot pass as a run that asks for nothing.
    client = FakeClient([], parsed=[])

    assert read_parsed(LLMRunner(client=cast("OpenAI", client), model="test"), cancelled) is None


def test_stops_a_parse_rather_than_retrying_it_for_a_run_left_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = Event()
    monkeypatch.setattr("jri.core.ai.llm_runner.sleep", lambda _: cancelled.set())
    # The double holds the rate limit and nothing after it. A retry finds nothing to read and fails the test.
    client = FakeClient([], parsed=[rate_limited()])

    assert read_parsed(LLMRunner(client=cast("OpenAI", client), model="test"), cancelled) is None


# JRI can point at any OpenAI-compatible provider, and not only at OpenAI.
# The pinned provider library defines no event with the name `response.reasoning.delta`.
# Another provider sends its reasoning under that name, so JRI reads all three names.
@pytest.mark.parametrize(
    "event_type", ["response.reasoning.delta", "response.reasoning_text.delta", "response.reasoning_summary_text.delta"]
)
def test_streams_the_reasoning_of_the_model(event_type: str) -> None:
    client = FakeClient([reasoning("weighing the options", event_type)])
    runner = LLMRunner(client=cast("OpenAI", client), model="test")

    assert list(runner.respond([]).events) == [ReasoningDelta("weighing the options")]


# Same reasoning as `test_streams_the_reasoning_of_the_model`: a non-OpenAI provider can use the unlisted event name.
@pytest.mark.parametrize(
    "event_type", ["response.reasoning.delta", "response.reasoning_text.delta", "response.reasoning_summary_text.delta"]
)
def test_streams_the_reasoning_of_a_parsed_call(event_type: str) -> None:
    runner = build_runner([
        thought("Weighing ", event_type),
        thought("the options.", event_type),
        *response(reply('{"answer": "ready"}')),
    ])

    thoughts, output = drain(runner.parse([], Output))

    assert thoughts == [ReasoningDelta("Weighing "), ReasoningDelta("the options.")]
    assert output == Output(answer="ready")


# The provider decides whether it sends reasoning.
# The same prompt can send many deltas or no deltas.
# No reasoning is the normal result for a call.
# It returns the same output as a call with reasoning.
# Rows show that the run is active.
def test_streams_nothing_for_a_parsed_call_that_published_no_reasoning() -> None:
    runner = build_runner(response(reply('{"answer": "ready"}')))

    thoughts, output = drain(runner.parse([], Output))

    assert thoughts == []
    assert output == Output(answer="ready")


def test_stops_a_parse_the_user_left_mid_thought() -> None:
    cancelled = Event()
    runner = build_runner(stopped_thinking(cancelled))

    thoughts, output = drain(runner.parse([], Output, cancelled))

    # Keep all reasoning that the model already sent.
    # A partial structured output has no valid result.
    assert thoughts == [ReasoningDelta("Weighing "), ReasoningDelta("the options.")]
    assert output is None


# A retry would send a second reasoning chain to one row.
# Do not retry after the reader receives reasoning.
# Report the error from the response that the reader received.
def test_does_not_retry_a_call_whose_thinking_reached_the_user(waits: list[float]) -> None:
    # The double holds the interrupted round and nothing after it. A retry finds nothing to read and fails the test.
    runner = build_runner(interrupted_thinking("Weighing the options."))

    with pytest.raises(ModelError, match="Rate limit reached"):
        drain(runner.parse([], Output))

    assert waits == []


# The provider reports usage when the response completes.
# A parsed call cannot report usage before it streams.
# The count that the cache served is the only sign of a cache that works, so a log line carries it.
def test_logs_the_context_a_call_spent(caplog: pytest.LogCaptureFixture) -> None:
    parsing = build_runner(response(reply('{"answer": "ready"}'), input_tokens=4321, cached_tokens=4000))
    replying = build_streaming_runner(response(reply("How often does it deploy?"), input_tokens=1234))

    with caplog.at_level(logging.INFO, logger="jri"):
        read_parsed(parsing)
        list(replying.respond([]).events)

    assert [record.getMessage() for record in caplog.records if record.getMessage().startswith("context_usage")] == [
        "context_usage input_tokens=4321 cached_tokens=4000",
        "context_usage input_tokens=1234 cached_tokens=0",
    ]


def test_streams_a_reply_the_provider_sent_whole() -> None:
    runner = build_streaming_runner(response(reply("How often does it deploy?")))

    assert list(runner.respond([]).events) == [TextDelta("How often does it deploy?")]


def test_reports_a_response_without_any_output() -> None:
    runner = build_runner(response())

    with pytest.raises(ModelError, match="did not contain a parsed output"):
        read_parsed(runner)


def test_reports_a_response_that_is_not_valid_json() -> None:
    runner = build_runner(response(reply("Sure! Here is the answer you asked for.")))

    with pytest.raises(ModelError, match="could not be read as Output"):
        read_parsed(runner)


# The provider library reads the structured answer during the stream. An answer that it cannot read comes from
# the model. The reader gets the words of JRI for that failure, and not the words of the library.
def test_reports_a_response_the_library_could_not_read_while_it_streamed() -> None:
    runner = build_runner(unreadable_answer(Output, '{"answer": "ready"} Hope that helps!'))

    with pytest.raises(ModelError, match="could not be read as Output"):
        read_parsed(runner)


def test_reports_why_a_response_was_cut_short() -> None:
    runner = build_runner(incomplete_response("max_output_tokens"))

    with pytest.raises(ModelError, match="incomplete: max_output_tokens"):
        read_parsed(runner)


def test_reports_an_unexplained_cut_short_response() -> None:
    runner = build_runner(incomplete_response(None))

    with pytest.raises(ModelError, match="incomplete: unknown reason"):
        read_parsed(runner)


# The error object holds the provider message, so a partial match accepts the object too. Match the full text.
def test_reports_why_a_response_failed() -> None:
    runner = build_runner(failed_response("the model overloaded"))

    with pytest.raises(ModelError, match=r"^the model overloaded$"):
        read_parsed(runner)


def test_reports_an_unexplained_failed_response() -> None:
    runner = build_runner(failed_response(None))

    with pytest.raises(ModelError, match="failed for an unknown reason"):
        read_parsed(runner)


@pytest.mark.parametrize("temperature", [0, None], ids=["configured", "omitted"])
def test_sends_temperature_only_when_configured(temperature: float | None) -> None:
    runner = LLMRunner(client=cast("OpenAI", FakeClient([])), model="test", temperature=temperature)

    # A provider rejects a temperature of `None`. The runner sends the sentinel that leaves the field out instead.
    assert runner.sampling == (omit if temperature is None else temperature)


# The provider supports the `max` effort level.
# The pinned provider library does not list this level.
# Send it so the accepted setting has an effect.
@pytest.mark.parametrize("effort", ["xhigh", "max"], ids=["listed", "unlisted"])
def test_sends_the_reasoning_effort_it_was_given(effort: ReasoningEffort) -> None:
    runner = LLMRunner(client=cast("OpenAI", FakeClient([])), model="test", reasoning_effort=effort)

    assert runner.reasoning == {"effort": effort, "summary": "auto"}


def test_rejects_a_context_over_the_input_size_limit() -> None:
    runner = LLMRunner(client=cast("OpenAI", FakeClient([])), model="test", max_input_size=10)

    with pytest.raises(ModelError, match="over the 10 byte limit"):
        drain(runner.parse([{"role": "user", "content": "far too many bytes"}], Output))


@pytest.mark.usefixtures("waits")
def test_retries_a_reply_the_provider_rate_limited() -> None:
    runner = build_streaming_runner(rate_limited(), streamed_reply("ready"))

    assert list(runner.respond([]).events) == [TextDelta("ready")]


def test_retries_a_reply_whose_connection_dropped(waits: list[float]) -> None:
    runner = build_streaming_runner(disconnection(), streamed_reply("ready"))

    assert list(runner.respond([]).events) == [TextDelta("ready")]
    assert waits == [2.0]


# JRI can point at any OpenAI-compatible provider. Each one names the delay it wants in the header it prefers, and
# counts it in the unit of that header.
@pytest.mark.parametrize(
    ("limit", "delay"),
    [(rate_limited(milliseconds="1157"), 1.157), (rate_limited(seconds="7"), 7.0)],
    ids=["milliseconds", "seconds"],
)
def test_waits_the_delay_the_provider_asked_for(waits: list[float], limit: "RateLimitError", delay: float) -> None:
    runner = build_streaming_runner(limit, streamed_reply("ready"))

    list(runner.respond([]).events)

    assert waits == [delay]


# A provider under maintenance can ask for an hour.
# A run must not wait that long, because the user cannot stop it during the wait.
def test_waits_no_longer_than_the_maximum_delay(waits: list[float]) -> None:
    runner = build_streaming_runner(rate_limited(milliseconds="3600000"), streamed_reply("ready"))

    list(runner.respond([]).events)

    assert waits == [30.0]


def test_waits_longer_after_each_rate_limit_left_unexplained(waits: list[float]) -> None:
    runner = build_streaming_runner(rate_limited(), rate_limited(), streamed_reply("ready"))

    list(runner.respond([]).events)

    assert waits == [2.0, 4.0]


def test_reports_a_rate_limit_that_outlasts_the_retries(waits: list[float]) -> None:
    runner = build_streaming_runner(*[rate_limited()] * 4)

    # A rate limit after all attempts is a provider condition.
    # It is not a JRI fault.
    # Keep the provider message format independent of this fact.
    with pytest.raises(ProviderUnavailableError, match="Rate limit reached"):
        list(runner.respond([]).events)

    assert waits == [2.0, 4.0, 8.0]


def test_reports_a_rejected_request_without_retrying(waits: list[float]) -> None:
    # The next response is what a retry would return.
    # The refusal proves that no retry occurred.
    runner = build_streaming_runner(
        rejection("Unsupported value: 'minimal' is not supported with `gpt-5.6-sol`."), streamed_reply("ready")
    )

    with pytest.raises(ProviderRefusalError, match="answered 400 Bad Request") as refusal:
        list(runner.respond([]).events)

    # This request gives the same refusal on every attempt.
    # Use its exception class to identify that condition.
    # Put the provider message in a JRI block.
    # Do not use the Python dictionary representation.
    assert str(refusal.value) == (
        f"The provider at {BASE_URL}/ answered 400 Bad Request, saying:\n"
        "<provider_answer>\nUnsupported value: 'minimal' is not supported with `gpt-5.6-sol`.\n</provider_answer>"
    )
    assert waits == []


# `Connection error.` gives no target address.
# It also gives no connection failure detail.
# Users need both details to diagnose the address.
def test_names_the_address_it_could_not_reach(waits: list[float]) -> None:
    dropped = disconnection(httpx.ConnectError("[Errno -2] Name or service not known"))
    runner = build_streaming_runner(*[dropped] * LLMRunner.MAX_ATTEMPTS)

    with pytest.raises(ProviderUnavailableError, match="Could not reach the provider") as failure:
        list(runner.respond([]).events)

    assert str(failure.value) == (f"Could not reach the provider at {BASE_URL}/: [Errno -2] Name or service not known")
    assert len(waits) == LLMRunner.MAX_ATTEMPTS - 1


# A gateway can reply instead of the provider.
# Its reply does not use the provider response format.
# Show its status and body for user diagnosis.
def test_passes_on_a_body_that_says_nothing_of_itself(waits: list[float]) -> None:
    outage = bad_gateway("<html><body><h1>502 Bad Gateway</h1></body></html>")
    runner = build_streaming_runner(*[outage] * LLMRunner.MAX_ATTEMPTS)

    with pytest.raises(ProviderUnavailableError, match="answered 502 Bad Gateway") as failure:
        list(runner.respond([]).events)

    assert str(failure.value) == (
        f"The provider at {BASE_URL}/ answered 502 Bad Gateway, saying:\n"
        "<provider_answer>\n<html><body><h1>502 Bad Gateway</h1></body></html>\n</provider_answer>"
    )
    assert len(waits) == LLMRunner.MAX_ATTEMPTS - 1


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

    assert read_parsed(runner) == Output(answer="ready")


class Output(BaseModel):
    answer: str
