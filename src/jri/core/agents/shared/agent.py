import inspect
import logging
from collections.abc import Generator
from typing import Any, cast

from openai import OpenAI
from openai.types.responses import ResponseInputItemParam, ResponseInputParam
from openai.types.shared import ReasoningEffort
from openai.types.shared_params import Reasoning

from .events import ChatEvent, ReasoningDelta, TextDelta, ToolCallFinished, ToolCallStarted
from .tool import Invocation, Tool

logger = logging.getLogger(__name__)


class Agent:
    """Minimal, customizable LLM agent.

    Subclass and decorate methods with ``@tool`` to expose them
    as function calls the LLM can invoke.
    """

    def __init__(
        self,
        *,
        client: OpenAI,
        model: str,
        sys_prompt: str,
        reasoning_effort: ReasoningEffort = None,
        initial_ctx: ResponseInputParam | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.reasoning_effort: ReasoningEffort = reasoning_effort
        self.tools = Tool.discover(self)
        self.sys_prompt = inspect.cleandoc(sys_prompt)
        self.ctx = list(initial_ctx or [])
        self.ctx.insert(0, {"role": "system", "content": self.sys_prompt})

    def send_message(self, message: str) -> Generator[ChatEvent]:
        """Send a user message and stream the response.

        Automatically handles tool-call loops: if the LLM requests
        function calls, the agent invokes them, and resumes the stream
        until the model produces a final text reply.

        Yields:
            Structured chat events from the streamed LLM response.

        Raises:
            RuntimeError: If the model reports a failed response.

        """
        self.ctx.append({"role": "user", "content": message})
        logger.info("message_started agent=%s model=%s", type(self).__name__, self.model)

        tool_definitions = [tool.definition for tool in self.tools]
        tools_by_name = {tool.name: tool for tool in self.tools}

        while True:
            outputs_by_index: dict[int, dict[str, Any]] = {}
            logger.info("request_started agent=%s input_items=%d", type(self).__name__, len(self.ctx))
            with self.client.responses.create(
                model=self.model,
                input=self.ctx,
                tools=tool_definitions,
                reasoning=Reasoning(effort=self.reasoning_effort, summary="auto"),
                stream=True,
            ) as stream:
                for response_event in stream:
                    response = cast("dict[str, Any]", response_event.to_dict())
                    match response["type"]:
                        case (
                            "response.reasoning.delta"
                            | "response.reasoning_text.delta"
                            | "response.reasoning_summary_text.delta"
                        ):
                            yield ReasoningDelta(response["delta"])
                        case "response.output_text.delta":
                            yield TextDelta(response["delta"])
                        case "response.output_item.done":
                            outputs_by_index[response["output_index"]] = response["item"]
                        case "response.completed" | "response.incomplete":
                            break
                        case "response.failed" | "error":
                            raise RuntimeError((response.get("error") or response["response"]["error"])["message"])
            outputs = [outputs_by_index[index] for index in sorted(outputs_by_index)]
            self.ctx.extend(cast("list[ResponseInputItemParam]", outputs))
            logger.info("request_finished agent=%s output_items=%d", type(self).__name__, len(outputs))
            response_outputs = [output for output in outputs if output["type"] != "function_call"]
            logger.debug("response outputs=%r", response_outputs)

            if all(output["type"] != "function_call" for output in outputs):
                logger.info("message_finished agent=%s", type(self).__name__)
                return

            for output in (output for output in outputs if output["type"] == "function_call"):
                name = output["name"]
                tool = tools_by_name.get(name)
                yield ToolCallStarted(
                    call_id=output["call_id"],
                    label=tool.format_label(tool.started_label, output["arguments"]) if tool else name,
                    symbol=tool.symbol if tool else "⚙︎",
                )
                invocation = tool.invoke(output["arguments"]) if tool else Invocation(f"Unknown tool `{name}`.")
                yield from invocation
                self.ctx.append({
                    "type": "function_call_output",
                    "call_id": output["call_id"],
                    "output": invocation.output,
                })
                yield ToolCallFinished(
                    call_id=output["call_id"],
                    label=tool.format_label(tool.finished_label, output["arguments"]) if tool else name,
                )
