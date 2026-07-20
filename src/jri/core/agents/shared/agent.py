import inspect
import logging
from collections.abc import Generator, Iterable
from dataclasses import InitVar, dataclass, field
from threading import Event
from typing import Any, cast

from openai import OpenAI
from openai.types.responses import ResponseInputItemParam, ResponseInputParam, ResponseStreamEvent
from openai.types.shared import ReasoningEffort
from openai.types.shared_params import Reasoning

from .events import ChatEvent, ReasoningDelta, TextDelta, ToolCallFinished, ToolCallStarted
from .tool import Invocation, Tool

logger = logging.getLogger(__name__)
MAX_ROUNDS = 50


@dataclass(kw_only=True)
class Agent:
    """Minimal, customizable LLM agent.

    Subclass and decorate methods with ``@tool`` to expose them
    as function calls the LLM can invoke.
    """

    initial_ctx: InitVar[ResponseInputParam | None] = None

    client: OpenAI
    model: str
    sys_prompt: str
    reasoning_effort: ReasoningEffort = None
    temperature: float | None = None

    tools: list[Tool] = field(init=False)
    history: ResponseInputParam = field(init=False)

    def __post_init__(self, initial_ctx: ResponseInputParam | None) -> None:
        self.tools = Tool.discover(self)
        self.sys_prompt = inspect.cleandoc(self.sys_prompt)
        self.history = list(initial_ctx or [])
        self.history.insert(0, {"role": "system", "content": self.sys_prompt})

    def get_context(self) -> ResponseInputParam:
        """Get the conversation context sent to the model.

        Returns:
            The conversation items as an ordered list.
        """

        return self.history

    def send_message(self, message: str, cancelled: Event | None = None) -> Generator[ChatEvent]:
        """Send a user message and stream the response.

        Automatically handles tool-call loops: if the LLM requests
        function calls, the agent invokes them, and resumes the stream
        until the model produces a final text reply.

        Yields:
            Structured chat events from the streamed LLM response.

        Raises:
            RuntimeError: If the model reports a failed response.

        """
        self.history.append({"role": "user", "content": message})
        cancelled = cancelled or Event()
        logger.info("message_started agent=%s model=%s", type(self).__name__, self.model)

        tool_definitions = [tool.definition for tool in self.tools]
        tools_by_name = {tool.name: tool for tool in self.tools}

        for _ in range(MAX_ROUNDS):
            outputs_by_index: dict[int, dict[str, Any]] = {}
            partial_text: list[str] = []
            context = self.get_context()
            logger.info("request_started agent=%s input_items=%d", type(self).__name__, len(context))
            with self.client.responses.create(
                model=self.model,
                input=context,
                tools=tool_definitions,
                reasoning=Reasoning(effort=self.reasoning_effort, summary="auto"),
                temperature=self.temperature,
                stream=True,
            ) as stream:
                for event in self._stream_response(stream, outputs_by_index):
                    if isinstance(event, TextDelta):
                        partial_text.append(event.text)
                    yield event
                    if cancelled.is_set():
                        break

            if cancelled.is_set():
                if partial_text:
                    self.history.append({"role": "assistant", "content": "".join(partial_text)})
                logger.info("message_cancelled agent=%s", type(self).__name__)
                return

            outputs = [outputs_by_index[index] for index in sorted(outputs_by_index)]
            self.history.extend(cast("list[ResponseInputItemParam]", outputs))
            logger.info("request_finished agent=%s output_items=%d", type(self).__name__, len(outputs))
            function_calls = [output for output in outputs if output.get("type") == "function_call"]

            if not function_calls:
                logger.info("message_finished agent=%s", type(self).__name__)
                return

            for output in function_calls:
                tool = tools_by_name.get(output["name"])
                yield from self._invoke(output, tool, cancelled)
                if cancelled.is_set():
                    logger.info("message_cancelled agent=%s", type(self).__name__)
                    return
        raise RuntimeError(f"Agent exceeded the limit of {MAX_ROUNDS} response rounds.")

    def _invoke(self, output: dict[str, Any], tool: Tool | None, cancelled: Event) -> Generator[ChatEvent]:
        name = output["name"]
        yield ToolCallStarted(
            call_id=output["call_id"],
            label=tool.format_label(tool.started_label, output["arguments"]) if tool else name,
            symbol=getattr(tool, "symbol", "⚙︎"),
        )
        if cancelled.is_set():
            self.history.append({
                "type": "function_call_output",
                "call_id": output["call_id"],
                "output": "Tool call cancelled.",
            })
            return
        invocation = tool.invoke(output["arguments"]) if tool else Invocation(f"Unknown tool `{name}`.")
        for event in invocation:
            yield event
            if cancelled.is_set():
                break
        self.history.append({"type": "function_call_output", "call_id": output["call_id"], "output": invocation.output})
        if not cancelled.is_set():
            yield ToolCallFinished(
                call_id=output["call_id"],
                label=tool.format_label(tool.finished_label, output["arguments"]) if tool else name,
            )

    def _stream_response(
        self, stream: Iterable[ResponseStreamEvent], outputs_by_index: dict[int, dict[str, Any]]
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
                        logger.info("context_usage agent=%s input_tokens=%d", type(self).__name__, usage.input_tokens)
                    return
                case "response.incomplete":
                    details = event.response.incomplete_details
                    raise RuntimeError(f"Model response incomplete: {details.reason if details else 'unknown reason'}")
                case "response.failed":
                    raise RuntimeError(str(event.response.error))
                case "error":
                    raise RuntimeError(event.message)
