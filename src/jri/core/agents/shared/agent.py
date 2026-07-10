import inspect
import json
from collections.abc import Generator
from threading import Event, Lock
from typing import Any, cast

import httpx
from openai import OpenAI, Stream
from openai.types.responses import FunctionToolParam, ResponseInputItemParam, ResponseInputParam
from openai.types.shared import ReasoningEffort
from openai.types.shared_params import Reasoning
from websocket import WebSocket, WebSocketConnectionClosedException, create_connection

from .events import ChatEvent, ModelIterationCompleted, ReasoningDelta, TextDelta, ToolCallFinished, ToolCallStarted
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
        self.cancellation_event = Event()
        self.tools = Tool.discover(self)
        self.sys_prompt = inspect.cleandoc(sys_prompt)
        self.ctx = list(initial_ctx or [])
        self.ctx.insert(0, {"role": "system", "content": self.sys_prompt})
        self.connection: WebSocket | None = None
        self.active_stream: Stream[Any] | None = None
        self.previous_response_id: str | None = None
        self.current_outputs: dict[int, dict[str, Any]] = {}
        self.partial_text = ""
        self.iteration_lock = Lock()

    def cancel(self) -> None:
        """Signal cancellation and prevent further agent work."""

        self.cancellation_event.set()

    def close(self) -> None:
        """Close active network resources."""

        stream = self.active_stream
        if stream:
            stream.close()
        connection = self.connection
        if connection:
            connection.close()
            if self.connection is connection:
                self.connection = None

    def send_message(self, message: str) -> Generator[ChatEvent | ModelIterationCompleted]:
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

        while not self.cancellation_event.is_set():
            with self.iteration_lock:
                self.current_outputs = {}
                self.partial_text = ""
            yield from self._stream_response(inputs, tool_definitions)
            with self.iteration_lock:
                if self.cancellation_event.is_set():
                    return
                outputs = [self.current_outputs[i] for i in sorted(self.current_outputs)]
                self.ctx.extend(cast("list[ResponseInputItemParam]", outputs))
                self.current_outputs = {}
                self.partial_text = ""
            yield ModelIterationCompleted()

            # Process tool calls, or return if none
            if all(output["type"] != "function_call" for output in outputs):
                return

            inputs = []
            for output in outputs:
                if self.cancellation_event.is_set():
                    return
                if output["type"] != "function_call":
                    continue
                name = output["name"]
                tool = tools_by_name.get(name)
                yield ToolCallStarted(
                    call_id=output["call_id"],
                    label=tool.format_label(tool.started_label, output["arguments"]) if tool else name,
                    symbol=tool.symbol if tool else "⚙︎",
                )
                invocation = (
                    tool.invoke(output["arguments"], self.cancellation_event)
                    if tool
                    else Invocation(f"Unknown tool `{name}`.")
                )
                yield from invocation
                if self.cancellation_event.is_set():
                    return
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
        self, inputs: list[ResponseInputItemParam], tools: list[FunctionToolParam]
    ) -> Generator[ReasoningDelta | TextDelta]:
        for event in self._receive_events(inputs, tools):
            if self.cancellation_event.is_set():
                return
            match event["type"]:
                case (
                    "response.reasoning.delta"
                    | "response.reasoning_text.delta"
                    | "response.reasoning_summary_text.delta"
                ):
                    yield ReasoningDelta(event["delta"])
                case "response.output_text.delta":
                    with self.iteration_lock:
                        self.partial_text += event["delta"]
                    yield TextDelta(event["delta"])
                case "response.output_item.done":
                    with self.iteration_lock:
                        self.current_outputs[event["output_index"]] = event["item"]
                        if event["item"]["type"] == "message":
                            self.partial_text = ""
                case "response.completed" | "response.incomplete":
                    self.previous_response_id = event["response"]["id"]
                    return
                case "response.failed" | "error":
                    if self.connection:
                        self.connection.close()
                        self.connection = None
                        self.previous_response_id = None
                    raise RuntimeError((event.get("error") or event["response"]["error"])["message"])

    def finish_cancelled_iteration(self) -> None:
        """Add completed and partial model output to the context."""

        with self.iteration_lock:
            self.ctx.extend(
                cast(
                    "list[ResponseInputItemParam]",
                    [self.current_outputs[index] for index in sorted(self.current_outputs)],
                )
            )
            if self.partial_text:
                self.ctx.append({"role": "assistant", "content": self.partial_text})
            self.current_outputs = {}
            self.partial_text = ""

    def _receive_events(
        self, inputs: list[ResponseInputItemParam], tools: list[FunctionToolParam]
    ) -> Generator[dict[str, Any]]:
        reasoning = Reasoning(effort=self.reasoning_effort, summary="auto")
        if self.cancellation_event.is_set():
            return
        if self.client.base_url.host != "api.openai.com":
            stream = self.client.responses.create(
                model=self.model, input=self.ctx, tools=tools, reasoning=reasoning, stream=True
            )
            self.active_stream = stream
            try:
                if self.cancellation_event.is_set():
                    return
                for event in stream:
                    yield cast("dict[str, Any]", event.to_dict())
            except httpx.HTTPError:
                if not self.cancellation_event.is_set():
                    raise
            finally:
                stream.close()
                if self.active_stream is stream:
                    self.active_stream = None
            return
        yield from self._receive_websocket_events(inputs, tools, reasoning)

    def _receive_websocket_events(
        self, inputs: list[ResponseInputItemParam], tools: list[FunctionToolParam], reasoning: Reasoning
    ) -> Generator[dict[str, Any]]:
        can_retry = True
        while True:
            if not self.connection:
                url = str(self.client.base_url).rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
                self.connection = create_connection(f"{url}/responses", header=self.client.auth_headers)
                self.previous_response_id = None
            connection = self.connection
            if self.cancellation_event.is_set():
                connection.close()
                if self.connection is connection:
                    self.connection = None
                return

            request = json.dumps({
                "type": "response.create",
                "model": self.model,
                "store": False,
                "input": inputs if self.previous_response_id else self.ctx,
                "tools": tools,
                "previous_response_id": self.previous_response_id,
                "reasoning": reasoning,
            })
            if not self._send_websocket_response(connection, request):
                return

            while payload := self._receive_websocket_payload(connection):
                event = json.loads(payload)
                can_retry &= event["type"] not in {"response.output_text.delta", "response.output_item.done"}
                yield event

            connection.close()
            if self.connection is connection:
                self.connection = None
            self.previous_response_id = None
            if self.cancellation_event.is_set():
                return
            if not can_retry:
                raise RuntimeError("OpenAI closed the WebSocket before completing the response.")
            can_retry = False

    def _send_websocket_response(self, connection: WebSocket, request: str) -> bool:
        try:
            connection.send(request)
        except WebSocketConnectionClosedException:
            if self.cancellation_event.is_set():
                return False
            raise
        return True

    def _receive_websocket_payload(self, connection: WebSocket) -> str | bytes | None:
        try:
            return connection.recv() or None
        except WebSocketConnectionClosedException:
            if self.cancellation_event.is_set():
                return None
            raise
