import inspect
import json
from collections.abc import Generator
from typing import Any, cast

from openai import OpenAI
from openai.types.responses import FunctionToolParam, ResponseInputItemParam, ResponseInputParam
from openai.types.shared import ReasoningEffort
from openai.types.shared_params import Reasoning
from websocket import WebSocket, create_connection

from .events import ChatEvent, ReasoningDelta, TextDelta, ToolCallFinished, ToolCallStarted
from .tool import Invocation, Tool


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
        self.connection: WebSocket | None = None
        self.previous_response_id: str | None = None

    def send_message(self, message: str) -> Generator[ChatEvent]:
        """Send a user message and stream the response.

        Automatically handles tool-call loops: if the LLM requests
        function calls, the agent invokes them, and resumes the stream
        until the model produces a final text reply.

        Yields:
            Structured chat events from the streamed LLM response.

        """
        inputs: list[ResponseInputItemParam] = [{"role": "user", "content": message}]
        self.ctx.extend(inputs)

        tool_definitions = [tool.definition for tool in self.tools]
        tools_by_name = {tool.name: tool for tool in self.tools}

        while True:
            outputs_by_idx: dict[int, dict[str, Any]] = {}
            yield from self._stream_response(inputs, tool_definitions, outputs_by_idx)
            outputs = [outputs_by_idx[i] for i in sorted(outputs_by_idx)]
            self.ctx.extend(cast("list[ResponseInputItemParam]", outputs))

            # Process tool calls, or return if none
            if all(output["type"] != "function_call" for output in outputs):
                return

            inputs = []
            for output in outputs:
                if output["type"] != "function_call":
                    continue
                name = output["name"]
                tool = tools_by_name.get(name)
                yield ToolCallStarted(
                    call_id=output["call_id"],
                    label=tool.format_label(tool.started_label, output["arguments"]) if tool else name,
                    symbol=tool.symbol if tool else "⚙︎",
                )
                invocation = tool.invoke(output["arguments"]) if tool else Invocation(f"Unknown tool `{name}`.")
                yield from invocation
                inputs.append({
                    "type": "function_call_output",
                    "call_id": output["call_id"],
                    "output": invocation.output,
                })
                self.ctx.append(inputs[-1])
                yield ToolCallFinished(
                    call_id=output["call_id"],
                    label=tool.format_label(tool.finished_label, output["arguments"]) if tool else name,
                )

    def _stream_response(
        self,
        inputs: list[ResponseInputItemParam],
        tools: list[FunctionToolParam],
        outputs_by_idx: dict[int, dict[str, Any]],
    ) -> Generator[ReasoningDelta | TextDelta]:
        for event in self._receive_events(inputs, tools):
            match event["type"]:
                case (
                    "response.reasoning.delta"
                    | "response.reasoning_text.delta"
                    | "response.reasoning_summary_text.delta"
                ):
                    yield ReasoningDelta(event["delta"])
                case "response.output_text.delta":
                    yield TextDelta(event["delta"])
                case "response.output_item.done":
                    outputs_by_idx[event["output_index"]] = event["item"]
                case "response.completed" | "response.incomplete":
                    self.previous_response_id = event["response"]["id"]
                    return
                case "response.failed" | "error":
                    if self.connection:
                        self.connection.close()
                        self.connection = None
                        self.previous_response_id = None
                    raise RuntimeError((event.get("error") or event["response"]["error"])["message"])

    def _receive_events(
        self, inputs: list[ResponseInputItemParam], tools: list[FunctionToolParam]
    ) -> Generator[dict[str, Any]]:
        reasoning = Reasoning(effort=self.reasoning_effort, summary="auto")
        if self.client.base_url.host != "api.openai.com":
            for event in self.client.responses.create(
                model=self.model, input=self.ctx, tools=tools, reasoning=reasoning, stream=True
            ):
                yield cast("dict[str, Any]", event.to_dict())
            return

        can_retry = True
        while True:
            if not self.connection:
                url = str(self.client.base_url).rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
                self.connection = create_connection(f"{url}/responses", header=self.client.auth_headers)
                self.previous_response_id = None

            self.connection.send(
                json.dumps({
                    "type": "response.create",
                    "model": self.model,
                    "store": False,
                    "input": inputs if self.previous_response_id else self.ctx,
                    "tools": tools,
                    "previous_response_id": self.previous_response_id,
                    "reasoning": reasoning,
                })
            )

            while payload := self.connection.recv():
                event = json.loads(payload)
                can_retry &= event["type"] not in {"response.output_text.delta", "response.output_item.done"}
                yield event

            self.connection.close()
            self.connection = None
            self.previous_response_id = None
            if not can_retry:
                raise RuntimeError("OpenAI closed the WebSocket before completing the response.")
            can_retry = False
