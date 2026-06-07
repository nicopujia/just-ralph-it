from collections.abc import Generator, Iterable
from typing import TYPE_CHECKING

from openai import OpenAI

if TYPE_CHECKING:
    from openai.types.responses.easy_input_message_param import (
        EasyInputMessageParam,
    )
    from openai.types.responses.response_input_param import ResponseInputParam
    from openai.types.responses.tool_param import ToolParam


FIRST_MESSAGE = "What do you want to build?"


class Interviewer:
    def __init__(
        self,
        client: OpenAI,
        model: str,
    ) -> None:
        self.client: OpenAI = client
        self.model: str = model
        self.messages: ResponseInputParam = [
            {
                "role": "assistant",
                "content": FIRST_MESSAGE,
            },
        ]
        self.tools: Iterable[ToolParam] = [
            {
                "type": "web_search",
            },
        ]

    def send_message(self, message: str) -> Generator[str]:
        user_message: EasyInputMessageParam = {
            "role": "user",
            "content": message,
        }
        self.messages.append(user_message)

        chunks: list[str] = []
        stream = self.client.responses.create(
            model=self.model,
            input=self.messages,
            tools=self.tools,
            stream=True,
        )
        for event in stream:
            if event.type == "response.output_text.delta":
                chunks.append(event.delta)
                yield event.delta

        agent_message: EasyInputMessageParam = {
            "role": "assistant",
            "content": "".join(chunks),
        }
        self.messages.append(agent_message)
