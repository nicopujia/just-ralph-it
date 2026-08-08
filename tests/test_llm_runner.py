import re
from collections.abc import Iterator
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, cast

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
from jri.core.exceptions import ModelError, UsageLimitError
from jri.core.notes import Notebook
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
    stopped_stream,
    streamed_reply,
)
from tests.doubles.settings import build_settings

if TYPE_CHECKING:
    from openai import OpenAI, OpenAIError

    from tests.doubles.openai import Round

# The depths a prompt line is written at: a heading at the margin, a
# bullet under it, and the continuation of a bullet that wrapped.
PROMPT_INDENTS = (0, 4, 6)
# Ruff bounds a source line at 120 columns, and a prompt line spends
# four of them on the indentation of the shallowest block that can
# hold it, two on its quotes and two on the `\n` it ends with, so a
# line wider than this is two literals that ran together.
PROMPT_MAX_WIDTH = 112
PROMPT_SECTION = re.compile(r"[A-Z][A-Za-z ]*:")


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []
    monkeypatch.setattr("jri.core.ai.llm_runner.sleep", delays.append)
    return delays


def build_runner(parsed: object) -> LLMRunner:
    return LLMRunner(client=cast("OpenAI", FakeClient([], parsed=[parsed])), model="test")


def build_streaming_runner(*rounds: "Round | OpenAIError") -> LLMRunner:
    return LLMRunner(client=cast("OpenAI", FakeClient(rounds)), model="test")


def build_agents(path: Path) -> list[Explorer | Interviewer]:
    settings = build_settings(FakeClient([]), search_api_key="BRAVE_SEARCH_API_KEY")
    # The Explorer writes its working directory into its prompt, so a
    # directory of this machine's would leave the width of one line to
    # whatever `tmp_path` happens to be.
    return [Interviewer(settings, Notebook(path / "notebook.yaml")), Explorer(settings, Path("/jri"))]


def build_prompts(path: Path) -> dict[str, str]:
    settings = build_settings(FakeClient([]))
    return {
        **{type(agent).__name__: agent.prompt for agent in build_agents(path)},
        "Architect": architect.Architect(settings).runner.prompt,
        "Architect.FINAL_PROMPT": architect.Architect.FINAL_PROMPT,
        "FunctionalAnalyst": functional_analyst.FunctionalAnalyst(settings).runner.prompt,
    }


# Each line with the ones it stands between, and `None` where the
# prompt begins or ends, so a rule about what a line sits under tells
# the top of a document from a line sitting under a blank one.
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


def test_wraps_every_prompt_line_inside_the_source_line_holding_it(tmp_path: Path) -> None:
    lines = read_prompt_lines(tmp_path)

    assert [(label, len(line)) for label, _, line, _ in lines if len(line) > PROMPT_MAX_WIDTH] == []


def test_separates_the_words_of_every_prompt_line_with_one_space(tmp_path: Path) -> None:
    lines = read_prompt_lines(tmp_path)

    assert [(label, line) for label, _, line, _ in lines if "  " in line.lstrip(" ") or line != line.rstrip()] == []


def test_indents_every_prompt_line_to_a_depth_the_document_uses(tmp_path: Path) -> None:
    lines = read_prompt_lines(tmp_path)

    assert [
        (label, line) for label, _, line, _ in lines if line and len(line) - len(line.lstrip(" ")) not in PROMPT_INDENTS
    ] == []


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
    assert build_runner(Output(answer="ready")).parse([], Output) == Output(answer="ready")


def test_falls_back_to_the_aggregated_response_text() -> None:
    runner = build_runner(response(reply('{"answer": "aggregated"}')))

    assert runner.parse([], Output) == Output(answer="aggregated")


def test_falls_back_to_the_streamed_text() -> None:
    runner = build_runner(partial_reply('{"answer": "streamed"}'))

    assert runner.parse([], Output) == Output(answer="streamed")


def test_stops_a_parse_between_the_events_of_its_stream() -> None:
    cancelled = Event()

    assert build_runner(stopped_stream(cancelled)).parse([], Output, cancelled) is None


def test_asks_for_nothing_on_behalf_of_a_run_already_stopped() -> None:
    cancelled = Event()
    cancelled.set()
    client = FakeClient([], parsed=[Output(answer="ready")])

    assert LLMRunner(client=cast("OpenAI", client), model="test").parse([], Output, cancelled) is None
    assert client.responses.options == []


def test_stops_a_parse_rather_than_retrying_it_for_a_run_left_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = Event()
    monkeypatch.setattr("jri.core.ai.llm_runner.sleep", lambda _: cancelled.set())
    client = FakeClient([], parsed=[rate_limited(), Output(answer="ready")])

    assert LLMRunner(client=cast("OpenAI", client), model="test").parse([], Output, cancelled) is None
    assert len(client.responses.options) == 1


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

    assert runner.parse([], Output) == Output(answer="ready")


class Output(BaseModel):
    answer: str
