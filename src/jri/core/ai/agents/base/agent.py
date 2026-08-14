import logging
from collections.abc import Generator
from dataclasses import InitVar, dataclass, field
from threading import Event
from typing import TYPE_CHECKING, ClassVar, TypeVar, cast

from openai import OpenAI
from openai.types.responses import ResponseInputParam
from pydantic import BaseModel

from jri.core import ai
from jri.core.exceptions import ModelError
from jri.core.settings import ReasoningEffort
from jri.lib import prompt

from .tool import DEFAULT_SYMBOL, Invocation, Tool

if TYPE_CHECKING:
    from openai.types.responses import ResponseInputItemParam

logger = logging.getLogger(__name__)

Result = TypeVar("Result", bound=BaseModel)


@dataclass(kw_only=True)
class Agent:
    MAX_ROUNDS: ClassVar[int] = 50
    # A stopped reply ends where the user stopped it. Record the stop, because the text alone reads as a full reply.
    CANCELLATION_RECORD: ClassVar[str] = "User stopped last reply. Items before this message are all that happened."

    prompt: InitVar[str]
    initial_context: InitVar[ResponseInputParam | None] = None

    client: OpenAI
    model: str
    reasoning_effort: ReasoningEffort = None
    temperature: float | None = None
    max_input_size: int | None = None

    tools: list[Tool] = field(init=False)
    history: ResponseInputParam = field(init=False)
    runner: "ai.LLMRunner" = field(init=False)
    failed_call_ids: list[str] = field(init=False, default_factory=list)

    def __post_init__(self, prompt: str, initial_context: ResponseInputParam | None) -> None:
        self.tools = Tool.discover(self)
        self.runner = ai.LLMRunner(
            client=self.client,
            model=self.model,
            prompt=prompt,
            reasoning_effort=self.reasoning_effort,
            temperature=self.temperature,
            max_input_size=self.max_input_size,
        )
        self.history = list(initial_context or [])
        self.history.insert(0, {"role": "system", "content": self.runner.prompt})

    def get_context(self) -> ResponseInputParam:
        return self.history

    def send_message(self, message: str, cancelled: Event | None = None) -> Generator["ai.AgentEvent"]:
        self.history.append({"role": "user", "content": message})
        yield from self.respond(cancelled)

    def respond(self, cancelled: Event | None = None) -> Generator["ai.AgentEvent"]:
        cancelled = cancelled or Event()
        logger.info("message_started agent=%s model=%s", type(self).__name__, self.model)

        tool_definitions = [tool.definition for tool in self.tools]

        for _ in range(self.MAX_ROUNDS):
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
                self._record_cancellation()
                return

            self.history.extend(cast("list[ResponseInputItemParam]", response.outputs))
            logger.info("request_finished agent=%s output_items=%d", type(self).__name__, len(response.outputs))
            function_calls = [output for output in response.outputs if output.get("type") == "function_call"]

            if not function_calls:
                logger.info("message_finished agent=%s", type(self).__name__)
                return

            # Give each call in a round an output, including cancelled calls. The next request requires every output.
            for output in function_calls:
                yield from self._invoke(output, cancelled)
            if cancelled.is_set():
                self._record_cancellation()
                return
        raise ModelError(f"Agent exceeded the limit of {self.MAX_ROUNDS} response rounds.")

    # A structured-output sibling to `respond`, for an agent that must both call tools and return a typed result.
    # Each call starts a fresh turn from the system prompt alone, discarding any history from an earlier call.
    # Structured streaming carries no `TextDelta`; only a tool that itself leaks one could break that, and none does.
    def parse(
        self, message: str, output_type: type[Result], cancelled: Event | None = None
    ) -> Generator["ai.ReasoningDelta | ai.ToolCallStarted | ai.ToolCallFinished", None, Result | None]:
        cancelled = cancelled or Event()
        self.history = self.history[:1]
        self.history.append({"role": "user", "content": message})
        logger.info("parse_started agent=%s model=%s", type(self).__name__, self.model)

        tool_definitions = [tool.definition for tool in self.tools]

        for _ in range(self.MAX_ROUNDS):
            context = self.get_context()
            logger.info("request_started agent=%s input_items=%d", type(self).__name__, len(context))
            result = yield from self.runner.parse(context, output_type, cancelled, tools=tool_definitions)

            if result is None:
                self._record_cancellation()
                return None

            if not isinstance(result, ai.PendingToolCalls):
                logger.info("parse_finished agent=%s", type(self).__name__)
                return result

            self.history.extend(cast("list[ResponseInputItemParam]", result.outputs))
            logger.info("request_finished agent=%s output_items=%d", type(self).__name__, len(result.outputs))
            for output in result.outputs:
                if output.get("type") != "function_call":
                    continue
                yield from self._invoke(output, cancelled)
            if cancelled.is_set():
                self._record_cancellation()
                return None
        raise ModelError(f"Agent exceeded the limit of {self.MAX_ROUNDS} response rounds.")

    # A history item states what happened, not what to do next. The prompt owns what the agent does with a stop.
    def _record_cancellation(self) -> None:
        self.history.append({"role": "system", "content": self.CANCELLATION_RECORD})
        logger.info("message_cancelled agent=%s", type(self).__name__)

    def _invoke(
        self, output: dict[str, object], cancelled: Event
    ) -> Generator["ai.ReasoningDelta | ai.ToolCallStarted | ai.ToolCallFinished"]:
        name = cast("str", output["name"])
        arguments = cast("str", output["arguments"])
        call_id = cast("str", output["call_id"])
        # Read the tools on each call. A run can take one away, as the explorer does without a search key.
        tool = next((candidate for candidate in self.tools if candidate.name == name), None)
        # Do not open a row for a cancelled call.
        # Close each opened row before return to prevent removal by another process.
        # Yield the row before the call continues. The user can then stop the call.
        opened = not cancelled.is_set()
        if opened:
            yield ai.ToolCallStarted(
                call_id=call_id,
                label=tool.format_label(tool.started_label, arguments) if tool else name,
                symbol=tool.symbol if tool else DEFAULT_SYMBOL,
            )
        if cancelled.is_set():
            invocation = Invocation("Tool call cancelled.", failed=True)
        elif tool:
            invocation = tool.invoke(arguments)
        else:
            # The model provides this name. Quote it so it cannot state that this run supports an unknown tool.
            invocation = Invocation(prompt.render(tool_call_failed=f"Unknown tool `{name}`."), failed=True)
        for event in invocation:
            yield event
            if cancelled.is_set():
                break
        if invocation.outcome == "failed":
            self.failed_call_ids.append(call_id)
        self.history.append({"type": "function_call_output", "call_id": call_id, "output": invocation.output})
        if opened:
            yield ai.ToolCallFinished(
                call_id=call_id,
                label=tool.format_label(tool.finished_label, arguments) if tool else name,
                outcome="stopped" if cancelled.is_set() else invocation.outcome,
                detail="" if cancelled.is_set() else invocation.detail,
            )
