import json
import logging
from collections.abc import Generator, Iterable, Sequence
from dataclasses import dataclass
from inspect import cleandoc
from typing import Any, TypeVar, cast

from openai import Omit, OpenAI, omit
from openai.types.responses import FunctionToolParam, ResponseInputParam, ResponseStreamEvent
from openai.types.shared import ReasoningEffort
from openai.types.shared_params import Reasoning
from pydantic import BaseModel

from .events import ChatEvent, ReasoningDelta, TextDelta

logger = logging.getLogger(__name__)
Result = TypeVar("Result", bound=BaseModel)


@dataclass
class Response:
    events: Generator[ChatEvent]
    outputs_by_index: dict[int, dict[str, Any]]

    @property
    def outputs(self) -> list[dict[str, Any]]:
        return [self.outputs_by_index[index] for index in sorted(self.outputs_by_index)]


@dataclass(kw_only=True)
class LLMRunner:
    """Run and parse individual model requests."""

    client: OpenAI
    model: str
    prompt: str = ""
    reasoning_effort: ReasoningEffort = None
    temperature: float | None = None
    max_input_size: int | None = None
    """Byte bound on model input; `None` leaves it unbounded."""

    def __post_init__(self) -> None:
        self.prompt = cleandoc(self.prompt)

    @property
    def sampling(self) -> float | Omit:
        """Send a temperature only when one is configured.

        Returns:
            The configured temperature, or nothing for reasoning models
            that reject the parameter outright.
        """

        return omit if self.temperature is None else self.temperature

    def respond(self, context: ResponseInputParam, tools: Sequence[FunctionToolParam] = ()) -> Response:
        """Stream one model response.

        Returns:
            Its event stream and collected output items.
        """

        self._check_size(context)
        outputs_by_index: dict[int, dict[str, Any]] = {}
        return Response(self._respond(context, tools, outputs_by_index), outputs_by_index)

    def parse(self, context: ResponseInputParam, output_type: type[Result]) -> Result:
        """Parse one model response into the requested type.

        Returns:
            The parsed response.

        Raises:
            RuntimeError: If the context is over `max_input_size`, or
                the response failed, was cut short, or has no parsed
                output.
        """

        self._check_size(context)
        logger.info("parse_started model=%s input_items=%d", self.model, len(context))
        with self.client.responses.stream(
            model=self.model,
            input=context,
            text_format=output_type,
            reasoning=Reasoning(effort=self.reasoning_effort, summary="auto"),
            temperature=self.sampling,
        ) as stream:
            streamed_text = ""
            for event in stream:
                _diagnose(event)
                if event.type == "response.output_text.delta":
                    streamed_text += event.delta
            response = stream.get_final_response()
        if response.output_parsed is not None:
            parsed = response.output_parsed
        elif response.output_text:
            parsed = output_type.model_validate_json(response.output_text)
        elif streamed_text:
            parsed = output_type.model_validate_json(streamed_text)
        else:
            raise RuntimeError("Model response did not contain a parsed output.")
        logger.info("parse_finished model=%s", self.model)
        logger.debug("parse_output model=%s output=%r", self.model, parsed)
        return parsed

    def _respond(
        self,
        context: ResponseInputParam,
        tools: Sequence[FunctionToolParam],
        outputs_by_index: dict[int, dict[str, Any]],
    ) -> Generator[ChatEvent]:
        with self.client.responses.create(
            model=self.model,
            input=context,
            tools=tools,
            reasoning=Reasoning(effort=self.reasoning_effort, summary="auto"),
            temperature=self.sampling,
            stream=True,
        ) as stream:
            yield from self._decode(stream, outputs_by_index)

    @staticmethod
    def _decode(
        stream: Iterable[ResponseStreamEvent], outputs_by_index: dict[int, dict[str, Any]]
    ) -> Generator[ChatEvent]:
        for event in stream:
            match event.type:
                case (
                    "response.reasoning.delta"
                    | "response.reasoning_text.delta"
                    | "response.reasoning_summary_text.delta"
                ):
                    yield ReasoningDelta(event.delta)
                case "response.output_text.delta":
                    yield TextDelta(event.delta)
                case "response.output_item.done":
                    outputs_by_index[event.output_index] = cast("dict[str, Any]", event.item.to_dict())
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
            raise RuntimeError(f"Request context is {size} bytes, over the {self.max_input_size} byte limit.")


def _diagnose(event: ResponseStreamEvent) -> None:
    match event.type:
        case "response.incomplete":
            details = event.response.incomplete_details
            raise RuntimeError(f"Model response incomplete: {details.reason if details else 'unknown reason'}")
        case "response.failed":
            raise RuntimeError(str(event.response.error))
        case "error":
            raise RuntimeError(event.message)
