from __future__ import annotations

import logging
from dataclasses import InitVar, dataclass, field
from threading import Event
from typing import TYPE_CHECKING, cast

from jri.core import ai

from .tool import Invocation, Tool

if TYPE_CHECKING:
    from collections.abc import Generator

    from openai import OpenAI
    from openai.types.responses import ResponseInputItemParam, ResponseInputParam
    from openai.types.shared import ReasoningEffort

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
    prompt: str
    reasoning_effort: ReasoningEffort = None
    temperature: float | None = None
    max_input_size: int | None = None

    tools: list[Tool] = field(init=False)
    history: ResponseInputParam = field(init=False)
    runner: ai.LLMRunner = field(init=False)
    failed_call_ids: list[str] = field(init=False, default_factory=list)

    def __post_init__(self, initial_ctx: ResponseInputParam | None) -> None:
        self.tools = Tool.discover(self)
        self.runner = ai.LLMRunner(
            client=self.client,
            model=self.model,
            prompt=self.prompt,
            reasoning_effort=self.reasoning_effort,
            temperature=self.temperature,
            max_input_size=self.max_input_size,
        )
        self.prompt = self.runner.prompt
        self.history = list(initial_ctx or [])
        self.history.insert(0, {"role": "system", "content": self.prompt})

    def get_context(self) -> ResponseInputParam:
        """Get the conversation context sent to the model.

        Returns:
            The conversation items as an ordered list.
        """

        return self.history

    def send_message(self, message: str, cancelled: Event | None = None) -> Generator[ai.ChatEvent]:
        """Send a user message and stream the response.

        Automatically handles tool-call loops: if the LLM requests
        function calls, the agent invokes them, and resumes the stream
        until the model produces a final text reply.

        Yields:
            Structured chat events from the streamed LLM response.

        """
        self.history.append({"role": "user", "content": message})
        yield from self.respond(cancelled)

    def respond(self, cancelled: Event | None = None) -> Generator[ai.ChatEvent]:
        """Respond to the current conversation context.

        Yields:
            Structured chat events from the streamed LLM response.

        Raises:
            RuntimeError: If the model reports a failed response.
        """

        cancelled = cancelled or Event()
        logger.info("message_started agent=%s model=%s", type(self).__name__, self.model)

        tool_definitions = [tool.definition for tool in self.tools]
        tools_by_name = {tool.name: tool for tool in self.tools}

        for _ in range(MAX_ROUNDS):
            partial_text: list[str] = []
            context = self.get_context()
            logger.info("request_started agent=%s input_items=%d", type(self).__name__, len(context))
            response = self.runner.respond(context, tool_definitions)
            for event in response.events:
                if isinstance(event, ai.TextDelta):
                    partial_text.append(event.text)
                yield event
                if cancelled.is_set():
                    response.events.close()
                    break

            if cancelled.is_set():
                if partial_text:
                    self.history.append({"role": "assistant", "content": "".join(partial_text)})
                logger.info("message_cancelled agent=%s", type(self).__name__)
                return

            self.history.extend(cast("list[ResponseInputItemParam]", response.outputs))
            logger.info("request_finished agent=%s output_items=%d", type(self).__name__, len(response.outputs))
            function_calls = [output for output in response.outputs if output.get("type") == "function_call"]

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

    def _invoke(self, output: dict[str, object], tool: Tool | None, cancelled: Event) -> Generator[ai.ChatEvent]:
        name = cast("str", output["name"])
        arguments = cast("str", output["arguments"])
        call_id = cast("str", output["call_id"])
        yield ai.ToolCallStarted(
            call_id=call_id,
            label=tool.format_label(tool.started_label, arguments) if tool else name,
            symbol=getattr(tool, "symbol", "⚙︎"),
        )
        if cancelled.is_set():
            self.failed_call_ids.append(call_id)
            self.history.append({"type": "function_call_output", "call_id": call_id, "output": "Tool call cancelled."})
            return
        invocation = tool.invoke(arguments) if tool else Invocation(f"Unknown tool `{name}`.", failed=True)
        for event in invocation:
            yield event
            if cancelled.is_set():
                break
        if invocation.failed:
            self.failed_call_ids.append(call_id)
        self.history.append({"type": "function_call_output", "call_id": call_id, "output": invocation.output})
        if not cancelled.is_set():
            yield ai.ToolCallFinished(
                call_id=call_id, label=tool.format_label(tool.finished_label, arguments) if tool else name
            )
