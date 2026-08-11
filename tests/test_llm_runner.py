import logging
import re
from collections.abc import Generator, Iterator
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from openai import omit
from pydantic import BaseModel

from jri.core.ai import (
    BLOCK_NOTICE,
    Explorer,
    Interviewer,
    LLMRunner,
    ReasoningDelta,
    TextDelta,
    architect,
    functional_analyst,
)
from jri.core.exceptions import ModelError, ProviderRefusalError, ProviderUnavailableError, UsageLimitError
from jri.core.notes import Notebook
from jri.core.settings import ReasoningEffort
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
)
from tests.doubles.settings import build_settings

if TYPE_CHECKING:
    from openai import OpenAI, OpenAIError

    from tests.doubles.openai import Round

PROMPT_SECTION = re.compile(r"[A-Z][A-Za-z ]*:")


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []
    monkeypatch.setattr("jri.core.ai.llm_runner.sleep", delays.append)
    return delays


def build_runner(parsed: object) -> LLMRunner:
    return LLMRunner(client=cast("OpenAI", FakeClient([], parsed=[parsed])), model="test")


# A parsed call sends model reasoning as a stream.
# Its return value is the parsed output.
# Read all reasoning before reading that output.
def drain(parse: Generator[ReasoningDelta, None, "Output | None"]) -> tuple[list[ReasoningDelta], "Output | None"]:
    thoughts: list[ReasoningDelta] = []
    while True:
        try:
            thoughts.append(next(parse))
        except StopIteration as stop:
            return thoughts, cast("Output | None", stop.value)


def read_parsed(runner: LLMRunner, cancelled: Event | None = None) -> "Output | None":
    return drain(runner.parse([], Output, cancelled))[1]


def build_streaming_runner(*rounds: "Round | OpenAIError") -> LLMRunner:
    return LLMRunner(client=cast("OpenAI", FakeClient(rounds)), model="test")


def build_agents(path: Path) -> list[Explorer | Interviewer]:
    settings = build_settings(FakeClient([]), search_api_key="BRAVE_SEARCH_API_KEY")
    # Explorer puts its working directory in the prompt.
    # A local temporary directory would make its text variable between runs.
    # Use `/jri` so the prompt is fixed.
    return [Interviewer(settings, Notebook(path / "notebook.yaml")), Explorer(settings, Path("/jri"))]


def build_prompts(path: Path) -> dict[str, str]:
    settings = build_settings(FakeClient([]))
    return {
        **{type(agent).__name__: agent.runner.prompt for agent in build_agents(path)},
        "Architect": architect.Architect(settings).runner.prompt,
        "Architect.FINAL_PROMPT": architect.Architect.FINAL_PROMPT,
        "FunctionalAnalyst": functional_analyst.FunctionalAnalyst(settings).runner.prompt,
    }


# Return each line with its adjacent lines.
# Use `None` for the start and end of the prompt.
# This distinguishes a document header from text after a blank line.
def read_prompt_lines(path: Path) -> Iterator[tuple[str, str | None, str, str | None]]:
    for name, text in build_prompts(path).items():
        lines = text.split("\n")
        for number, line in enumerate(lines):
            above = lines[number - 1] if number else None
            below = lines[number + 1] if number + 1 < len(lines) else None
            yield f"{name}:{number + 1}", above, line, below


def test_sends_a_prompt_exactly_as_written_under_the_block_notice() -> None:
    written = "Role: Tester.\n\nConstraints:\n    - An indented line whose indentation is the prompt's own.\n"

    runner = LLMRunner(client=cast("OpenAI", FakeClient([])), model="test", prompt=written)

    assert runner.prompt == f"{written}\n\n{BLOCK_NOTICE}"


def test_separates_the_words_of_every_prompt_line_with_one_space(tmp_path: Path) -> None:
    lines = read_prompt_lines(tmp_path)

    assert [(label, line) for label, _, line, _ in lines if "  " in line or line != line.rstrip()] == []


def test_starts_every_prompt_line_at_the_margin(tmp_path: Path) -> None:
    lines = read_prompt_lines(tmp_path)

    assert [(label, line) for label, _, line, _ in lines if line.startswith(" ")] == []


def test_opens_a_section_under_every_blank_line_of_a_prompt(tmp_path: Path) -> None:
    lines = read_prompt_lines(tmp_path)

    assert [
        (label, below) for label, _, line, below in lines if not line and (not below or below.startswith(" "))
    ] == []


def test_stands_every_section_of_a_prompt_under_a_blank_line(tmp_path: Path) -> None:
    lines = read_prompt_lines(tmp_path)

    assert [(label, line) for label, above, line, _ in lines if above and PROMPT_SECTION.match(line)] == []


def test_ends_every_prompt_at_the_last_line_it_wrote(tmp_path: Path) -> None:
    prompts = build_prompts(tmp_path)

    assert [name for name, text in prompts.items() if text != text.strip()] == []


def test_flows_every_tool_description_into_one_single_spaced_line(tmp_path: Path) -> None:
    descriptions = [capability.description for agent in build_agents(tmp_path) for capability in agent.tools]

    assert [text for text in descriptions if text != " ".join(text.split())] == []


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
    client = FakeClient([], parsed=[Output(answer="ready")])

    assert read_parsed(LLMRunner(client=cast("OpenAI", client), model="test"), cancelled) is None
    assert client.responses.options == []


def test_stops_a_parse_rather_than_retrying_it_for_a_run_left_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = Event()
    monkeypatch.setattr("jri.core.ai.llm_runner.sleep", lambda _: cancelled.set())
    client = FakeClient([], parsed=[rate_limited(), Output(answer="ready")])

    assert read_parsed(LLMRunner(client=cast("OpenAI", client), model="test"), cancelled) is None
    assert len(client.responses.options) == 1


# JRI can point at any OpenAI-compatible provider, not only OpenAI. `response.reasoning.delta` names no event the
# pinned provider library defines; another provider sends reasoning under it. Read all three names.
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
    client = FakeClient([], parsed=[interrupted_thinking("Weighing the options."), Output(answer="ready")])
    runner = LLMRunner(client=cast("OpenAI", client), model="test")

    with pytest.raises(ModelError, match="Rate limit reached"):
        drain(runner.parse([], Output))

    assert waits == []
    assert len(client.responses.options) == 1


# The provider reports usage when the response completes.
# A parsed call cannot report usage before it streams.
def test_logs_the_context_a_call_spent(caplog: pytest.LogCaptureFixture) -> None:
    parsing = build_runner(response(reply('{"answer": "ready"}'), input_tokens=4321))
    replying = build_streaming_runner(response(reply("How often does it deploy?"), input_tokens=1234))

    with caplog.at_level(logging.INFO, logger="jri"):
        read_parsed(parsing)
        list(replying.respond([]).events)

    assert [record.getMessage() for record in caplog.records if record.getMessage().startswith("context_usage")] == [
        "context_usage input_tokens=4321",
        "context_usage input_tokens=1234",
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

    with pytest.raises(ModelError):
        read_parsed(runner)


def test_reports_why_a_response_was_cut_short() -> None:
    runner = build_runner(incomplete_response("max_output_tokens"))

    with pytest.raises(ModelError, match="incomplete: max_output_tokens"):
        read_parsed(runner)


def test_reports_an_unexplained_cut_short_response() -> None:
    runner = build_runner(incomplete_response(None))

    with pytest.raises(ModelError, match="incomplete: unknown reason"):
        read_parsed(runner)


def test_reports_a_failed_response() -> None:
    runner = build_runner(failed_response("the model overloaded"))

    with pytest.raises(ModelError, match="the model overloaded"):
        read_parsed(runner)


@pytest.mark.parametrize("temperature", [0, None], ids=["configured", "omitted"])
def test_sends_temperature_only_when_configured(temperature: float | None) -> None:
    client = FakeClient([], parsed=[Output(answer="ready")])

    read_parsed(LLMRunner(client=cast("OpenAI", client), model="test", temperature=temperature))

    assert client.responses.options[-1]["temperature"] == (omit if temperature is None else temperature)


# The provider supports the `max` effort level.
# The pinned provider library does not list this level.
# Send it so the accepted setting has an effect.
@pytest.mark.parametrize("effort", ["xhigh", "max"], ids=["listed", "unlisted"])
def test_sends_the_reasoning_effort_it_was_given(effort: ReasoningEffort) -> None:
    client = FakeClient([], parsed=[Output(answer="ready")])

    read_parsed(LLMRunner(client=cast("OpenAI", client), model="test", reasoning_effort=effort))

    assert client.responses.options[-1]["reasoning"] == {"effort": effort, "summary": "auto"}


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

    # A rate limit after all attempts is a provider condition.
    # It is not a JRI fault.
    # Keep the provider message format independent of this fact.
    with pytest.raises(ProviderUnavailableError, match="Rate limit reached"):
        list(runner.respond([]).events)

    assert len(waits) == LLMRunner.MAX_ATTEMPTS - 1


def test_reports_a_rejected_request_without_retrying(waits: list[float]) -> None:
    # The next response is what a retry would return.
    # The refusal proves that no retry occurred.
    runner = build_streaming_runner(
        rejection("Unsupported value: 'minimal' is not supported with `gpt-5.6-sol`."), streamed_reply("ready")
    )

    with pytest.raises(ProviderRefusalError) as refusal:
        list(runner.respond([]).events)

    # This request gives the same refusal on every attempt.
    # Use its exception class to identify that condition.
    # Put the provider message in a JRI code block.
    # Do not use the Python dictionary representation.
    assert str(refusal.value) == (
        f"The provider at {BASE_URL}/ answered 400 Bad Request, saying:\n"
        "```\nUnsupported value: 'minimal' is not supported with `gpt-5.6-sol`.\n```"
    )
    assert waits == []


# `Connection error.` gives no target address.
# It also gives no connection failure detail.
# Users need both details to diagnose the address.
def test_names_the_address_it_could_not_reach(waits: list[float]) -> None:
    dropped = disconnection(httpx.ConnectError("[Errno -2] Name or service not known"))
    runner = build_streaming_runner(*[dropped] * LLMRunner.MAX_ATTEMPTS)

    with pytest.raises(ProviderUnavailableError) as failure:
        list(runner.respond([]).events)

    assert str(failure.value) == (f"Could not reach the provider at {BASE_URL}/: [Errno -2] Name or service not known")
    assert len(waits) == LLMRunner.MAX_ATTEMPTS - 1


# A gateway can reply instead of the provider.
# Its reply does not use the provider response format.
# Show its status and body for user diagnosis.
def test_passes_on_a_body_that_says_nothing_of_itself(waits: list[float]) -> None:
    outage = bad_gateway("<html><body><h1>502 Bad Gateway</h1></body></html>")
    runner = build_streaming_runner(*[outage] * LLMRunner.MAX_ATTEMPTS)

    with pytest.raises(ProviderUnavailableError) as failure:
        list(runner.respond([]).events)

    assert str(failure.value) == (
        f"The provider at {BASE_URL}/ answered 502 Bad Gateway, saying:\n"
        "```\n<html><body><h1>502 Bad Gateway</h1></body></html>\n```"
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
