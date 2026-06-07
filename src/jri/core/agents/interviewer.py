from collections.abc import Generator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from openai import OpenAI
from openai.types.responses.response_input_param import ResponseInputParam

if TYPE_CHECKING:
    from openai.types.responses.easy_input_message_param import (
        EasyInputMessageParam,
    )


@dataclass
class Interviewer:
    client: OpenAI
    model: str
    messages: ResponseInputParam = field(default_factory=list)

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
