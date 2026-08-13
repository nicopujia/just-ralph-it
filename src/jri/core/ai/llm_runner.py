import json
import logging
from collections.abc import Generator, Iterable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from threading import Event
from time import sleep
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from openai import APIConnectionError, APIStatusError, Omit, OpenAI, OpenAIError, omit
from openai.types.responses import FunctionToolParam, ResponseInputParam, ResponseStreamEvent
from openai.types.shared_params import Reasoning
from pydantic import BaseModel, ValidationError

from jri.core.exceptions import ModelError, ProviderRefusalError, ProviderUnavailableError, UsageLimitError
from jri.core.settings import ReasoningEffort
from jri.lib import prompt

from . import prompts
from .events import AgentEvent, ReasoningDelta, TextDelta

if TYPE_CHECKING:
    from openai.types.shared import ReasoningEffort as ProviderReasoningEffort

# A fence protects content only when the model has instructions for it. Add this notice to every prompt.
BLOCK_NOTICE = prompts.read("block_notice")

TRANSIENT_STATUSES = frozenset({HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.CONFLICT, HTTPStatus.TOO_MANY_REQUESTS})
"""Statuses a later attempt can still succeed on."""

EXHAUSTION_CODES = frozenset({"insufficient_quota", "usage_limit_reached", "billing_hard_limit_reached"})
"""Codes of a spent budget, which no amount of waiting refills."""

STATUS_PHRASES = {int(status): status.phrase for status in HTTPStatus}
"""What a status is called; a provider may use one nobody named."""

Result = TypeVar("Result", bound=BaseModel)

logger = logging.getLogger(__name__)


@dataclass
class Response:
    events: Generator[AgentEvent]
    outputs_by_index: dict[int, dict[str, Any]]

    @property
    def outputs(self) -> list[dict[str, Any]]:
        return [self.outputs_by_index[index] for index in sorted(self.outputs_by_index)]


# A structured round ended with tool calls instead of a final, schema-matching result.
@dataclass(frozen=True)
class PendingToolCalls:
    outputs: list[dict[str, Any]]


@dataclass(kw_only=True)
class LLMRunner:
    MAX_ATTEMPTS: ClassVar[int] = 4
    RETRY_DELAY: ClassVar[float] = 2.0
    """Seconds waited when the provider gives no hint; it doubles."""
    MAX_RETRY_DELAY: ClassVar[float] = 30.0

    client: OpenAI
    model: str
    prompt: str = ""
    reasoning_effort: ReasoningEffort = None
    temperature: float | None = None
    max_input_size: int | None = None
    """Byte bound on model input; `None` leaves it unbounded."""

    def __post_init__(self) -> None:
        self.prompt = f"{self.prompt}\n\n{BLOCK_NOTICE}"

    @property
    def sampling(self) -> float | Omit:
        return omit if self.temperature is None else self.temperature

    # Provider-library effort values are version-specific. Send the setting value and let the provider validate it.
    @property
    def reasoning(self) -> Reasoning:
        return Reasoning(effort=cast("ProviderReasoningEffort", self.reasoning_effort), summary="auto")

    def respond(self, context: ResponseInputParam, tools: Sequence[FunctionToolParam] = ()) -> Response:
        self._check_size(context)
        outputs_by_index: dict[int, dict[str, Any]] = {}
        return Response(self._respond(context, tools, outputs_by_index), outputs_by_index)

    # A stopped structured response has no result. Check for a stop before each retry to avoid a call for a stopped run.
    # Store the result in `parsed` because callers cannot read a generator return value while they drain its events.
    # A round that calls a tool returns `PendingToolCalls` instead of `output_type`; the caller runs the tools,
    # extends `context` with their outputs, and calls `parse` again for the next round — mirroring `respond`.
    def parse(
        self,
        context: ResponseInputParam,
        output_type: type[Result],
        cancelled: Event | None = None,
        tools: Sequence[FunctionToolParam] = (),
    ) -> Generator[ReasoningDelta, None, Result | PendingToolCalls | None]:
        self._check_size(context)
        cancelled = cancelled or Event()
        attempt = 1
        parsed: list[Result | PendingToolCalls | None] = []
        while not cancelled.is_set():
            streamed = False
            try:
                for thought in self._parse(context, output_type, tools, cancelled, parsed):
                    streamed = True
                    yield thought
            except OpenAIError as error:
                # Do not retry after streamed reasoning. A retry would show a second reasoning chain in the same row.
                if streamed:
                    raise self._read_failure(error) from error
                self._wait_to_retry(error, attempt)
                attempt += 1
            else:
                return parsed[-1]
        logger.info("parse_cancelled model=%s", self.model)
        return None

    def _respond(
        self,
        context: ResponseInputParam,
        tools: Sequence[FunctionToolParam],
        outputs_by_index: dict[int, dict[str, Any]],
    ) -> Generator[AgentEvent]:
        attempt = 1
        while True:
            streamed = False
            try:
                with self.client.responses.create(
                    model=self.model,
                    input=context,
                    tools=tools,
                    reasoning=self.reasoning,
                    temperature=self.sampling,
                    stream=True,
                ) as stream:
                    for event in self._decode(stream, outputs_by_index):
                        streamed = True
                        yield event
            except OpenAIError as error:
                # Do not retry a turn after it streams output. A retry would repeat text that the user saw.
                if streamed:
                    raise self._read_failure(error) from error
                outputs_by_index.clear()
                self._wait_to_retry(error, attempt)
                attempt += 1
            else:
                return

    def _parse(
        self,
        context: ResponseInputParam,
        output_type: type[Result],
        tools: Sequence[FunctionToolParam],
        cancelled: Event,
        parsed: list[Result | PendingToolCalls | None],
    ) -> Generator[ReasoningDelta]:
        logger.info("parse_started model=%s input_items=%d", self.model, len(context))
        outputs_by_index: dict[int, dict[str, Any]] = {}
        with self.client.responses.stream(
            model=self.model,
            input=context,
            tools=tools,
            text_format=output_type,
            reasoning=self.reasoning,
            temperature=self.sampling,
        ) as stream:
            streamed_text = ""
            for event in stream:
                _diagnose(event)
                match event.type:
                    # The provider can omit reasoning summaries.
                    # The rows still represent the run when no summary arrives.
                    case (
                        "response.reasoning.delta"
                        | "response.reasoning_text.delta"
                        | "response.reasoning_summary_text.delta"
                    ):
                        yield ReasoningDelta(event.delta)
                    case "response.output_text.delta":
                        streamed_text += event.delta
                    case "response.output_item.done":
                        outputs_by_index[event.output_index] = cast("dict[str, Any]", event.item.to_dict())
                    case "response.completed":
                        if usage := event.response.usage:
                            logger.info("context_usage input_tokens=%d", usage.input_tokens)
                # Check for a stop during structured streaming. Leaving this block closes the stream.
                # Do not request a final response from an unfinished stream because that waits for completion.
                if cancelled.is_set():
                    logger.info("parse_cancelled model=%s", self.model)
                    parsed.append(None)
                    return
            response = stream.get_final_response()
        function_calls = [item for item in outputs_by_index.values() if item.get("type") == "function_call"]
        if function_calls:
            logger.info("parse_tool_calls model=%s calls=%d", self.model, len(function_calls))
            parsed.append(PendingToolCalls([outputs_by_index[index] for index in sorted(outputs_by_index)]))
            return
        text = response.output_text or streamed_text
        if response.output_parsed is not None:
            result = response.output_parsed
        elif text:
            try:
                result = output_type.model_validate_json(text)
            except ValidationError as error:
                # Recover from invalid model output. Do not recover from an error raised by the model library.
                raise ModelError(f"Model response could not be read as {output_type.__name__}: {error}") from error
        else:
            raise ModelError("Model response did not contain a parsed output.")
        logger.info("parse_finished model=%s", self.model)
        logger.debug("parse_output model=%s output=%r", self.model, result)
        parsed.append(result)

    def _wait_to_retry(self, error: OpenAIError, attempt: int) -> None:
        if attempt >= self.MAX_ATTEMPTS or not _can_retry(error):
            raise self._read_failure(error) from error
        delay = min(_read_retry_hint(error) or self.RETRY_DELAY * 2 ** (attempt - 1), self.MAX_RETRY_DELAY)
        logger.warning("request_retrying model=%s attempt=%d delay=%.3f error=%s", self.model, attempt, delay, error)
        sleep(delay)

    # Convert provider exceptions to JRI failures here. A spent budget and a refusal cannot succeed after a retry.
    # A connection failure or provider fault can succeed later. Report only remaining JRI errors.
    def _read_failure(self, error: OpenAIError) -> ModelError:
        if isinstance(error, APIConnectionError):
            return ProviderUnavailableError(
                f"Could not reach the provider at {self.client.base_url}: {_read_cause(error)}"
            )
        if not isinstance(error, APIStatusError):
            return ModelError(str(error))
        # Quote provider text instead of presenting it as JRI text.
        # A response body cannot close the block that holds it.
        answer = (
            f"The provider at {self.client.base_url} answered {_name_status(error.status_code)}, saying:\n"
            f"{prompt.quote(_read_body(error), 'provider_answer')}"
        )
        if error.code in EXHAUSTION_CODES:
            return UsageLimitError(answer)
        return ProviderUnavailableError(answer) if _can_retry(error) else ProviderRefusalError(answer)

    @staticmethod
    def _decode(
        stream: Iterable[ResponseStreamEvent], outputs_by_index: dict[int, dict[str, Any]]
    ) -> Generator[AgentEvent]:
        streamed_indexes: set[int] = set()
        for event in stream:
            match event.type:
                case (
                    "response.reasoning.delta"
                    | "response.reasoning_text.delta"
                    | "response.reasoning_summary_text.delta"
                ):
                    yield ReasoningDelta(event.delta)
                case "response.output_text.delta":
                    streamed_indexes.add(event.output_index)
                    yield TextDelta(event.delta)
                case "response.output_item.done":
                    item = cast("dict[str, Any]", event.item.to_dict())
                    outputs_by_index[event.output_index] = item
                    # Record the stream result.
                    # If a message has no deltas, emit it here so the user sees the model reply.
                    if item.get("type") == "message" and event.output_index not in streamed_indexes:
                        parts = cast("list[dict[str, Any]]", item.get("content", []))
                        if text := "".join(part["text"] for part in parts if part.get("type") == "output_text"):
                            yield TextDelta(text)
                case "response.completed":
                    if usage := event.response.usage:
                        logger.info("context_usage input_tokens=%d", usage.input_tokens)
                    return
                case _:
                    _diagnose(event)

    def _check_size(self, context: ResponseInputParam) -> None:
        if self.max_input_size is None:
            return
        size = len(json.dumps(context).encode())
        if size > self.max_input_size:
            raise ModelError(f"Request context is {size} bytes, over the {self.max_input_size} byte limit.")


# The library uses `Connection error.` for every transport failure. The transport error identifies the actual cause.
def _read_cause(error: APIConnectionError) -> str:
    return str(error.__cause__ or "").strip() or error.message


def _name_status(status: int) -> str:
    return f"{status} {STATUS_PHRASES[status]}" if status in STATUS_PHRASES else str(status)


# Return the provider message when available. Otherwise, return the complete body without a summary.
# Log the raw exception in both cases.
def _read_body(error: APIStatusError) -> str:
    body = error.body
    if isinstance(body, dict) and isinstance(message := body.get("message"), str) and message.strip():
        return message
    if body is None:
        return error.message
    return body if isinstance(body, str) else json.dumps(body, indent=2, ensure_ascii=False)


def _can_retry(error: OpenAIError) -> bool:
    if isinstance(error, APIConnectionError):
        return True
    if not isinstance(error, APIStatusError) or error.code in EXHAUSTION_CODES:
        return False
    return error.status_code in TRANSIENT_STATUSES or error.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR


def _read_retry_hint(error: OpenAIError) -> float | None:
    if not isinstance(error, APIStatusError):
        return None
    for header, seconds_per_unit in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = error.response.headers.get(header)
        try:
            return float(cast("str", value)) * seconds_per_unit
        except (TypeError, ValueError):
            continue
    return None


def _diagnose(event: ResponseStreamEvent) -> None:
    match event.type:
        case "response.incomplete":
            details = event.response.incomplete_details
            raise ModelError(f"Model response incomplete: {details.reason if details else 'unknown reason'}")
        case "response.failed":
            raise ModelError(str(event.response.error))
        case "error":
            raise ModelError(event.message)
