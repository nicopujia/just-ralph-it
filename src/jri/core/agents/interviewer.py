from collections.abc import Generator
from typing import override

from openai import OpenAI

from .base import Agent

FIRST_MESSAGE = "What do you want to build?"


class Interviewer(Agent):
    def __init__(self, client: OpenAI, model: str) -> None:
        super().__init__(
            model=model,
            client=client,
            initial_context_window=[
                {"role": "assistant", "content": FIRST_MESSAGE},
            ],
            tools=[{"type": "web_search"}],
        )

    @override
    def send_message(self, message: str) -> Generator[str]:
        self.context_window.append({
            "role": "user",
            "content": message,
        })

        chunks: list[str] = []
        stream = self.client.responses.create(
            model=self.model,
            input=self.context_window,
            tools=self.tools,
            stream=True,
        )
        for event in stream:
            if event.type == "response.output_text.delta":
                chunks.append(event.delta)
                yield event.delta

        self.context_window.append({
            "role": "assistant",
            "content": "".join(chunks),
        })
