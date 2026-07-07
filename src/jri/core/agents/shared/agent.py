from collections.abc import Generator
from typing import cast

from openai import OpenAI
from openai.types.responses import ResponseInputItemParam, ResponseInputParam, ResponseOutputItem

from jri.lib.text import unwrap_prose

from .events import ChatEvent, TextDelta, ToolCallFinished, ToolCallStarted
from .tool import Tool


class Agent:
    """Minimal, customizable LLM agent.

    Subclass and decorate methods with ``@tool`` to expose them
    as function calls the LLM can invoke.
    """

    def __init__(
        self, *, client: OpenAI, model: str, sys_prompt: str, initial_ctx: ResponseInputParam | None = None
    ) -> None:
        self.client = client
        self.model = model
        self.tools = Tool.get_list_from_owner(self)
        self.sys_prompt = unwrap_prose(sys_prompt)
        self.reset_context(initial_ctx)

    def reset_context(self, initial_ctx: ResponseInputParam | None = None) -> None:
        self.ctx = list(initial_ctx or [])
        self.ctx.insert(0, {"role": "system", "content": self.sys_prompt})

    def send_message(self, message: str) -> Generator[ChatEvent]:
        """Send a user message and stream the response.

        Automatically handles tool-call loops: if the LLM requests
        function calls, the agent invokes them, and resumes the stream
        until the model produces a final text reply.

        Yields:
            Structured chat events from the streamed LLM response.
        """
        user_item: ResponseInputItemParam = {"role": "user", "content": message}
        turn_items: list[ResponseInputItemParam] = [user_item]
        self.ctx.append(user_item)

        tool_definitions = [tool.definition for tool in self.tools]
        tools_by_name = {tool.name: tool for tool in self.tools}

        while True:
            outputs_by_idx: dict[int, ResponseOutputItem] = {}

            # Call the model
            stream = self.client.responses.create(model=self.model, input=self.ctx, tools=tool_definitions, stream=True)
            for event in stream:
                match event.type:
                    case "response.output_text.delta":
                        yield TextDelta(event.delta)
                    case "response.output_item.done":
                        outputs_by_idx[event.output_index] = event.item
                    case _:
                        pass

            outputs = [outputs_by_idx[i] for i in sorted(outputs_by_idx)]
            output_items = cast("list[ResponseInputItemParam]", outputs)
            self.ctx.extend(output_items)
            turn_items.extend(output_items)

            # Process tool calls, or return if none
            if all(output.type != "function_call" for output in outputs):
                return

            for output in outputs:
                if output.type != "function_call":
                    continue
                yield ToolCallStarted(call_id=output.call_id, tool_name=output.name)
                tool = tools_by_name.get(output.name)
                call_output: ResponseInputItemParam = {
                    "type": "function_call_output",
                    "call_id": output.call_id,
                    "output": (tool.invoke(output.arguments) if tool else f"Unknown tool `{output.name}`."),
                }
                self.ctx.append(call_output)
                turn_items.append(call_output)
                self.after_tool_call(output.name, turn_items)
                yield ToolCallFinished(call_id=output.call_id)

    def after_tool_call(self, _tool_name: str, _turn_items: list[ResponseInputItemParam]) -> None:
        """Allow subclasses to react after tool results."""
