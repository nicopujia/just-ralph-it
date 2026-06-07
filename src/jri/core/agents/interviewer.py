from dataclasses import dataclass, field
from enum import StrEnum

from openai import OpenAI


class MessageSource(StrEnum):
    USER = "user"
    AGENT = "agent"


@dataclass
class Interviewer:
    client: OpenAI
    model: str
    messages: list[tuple[MessageSource, str]] = field(default_factory=list)

    def send_message(self, message: str) -> str:
        self.messages.append((MessageSource.USER, message))
        response = self.client.responses.create(
            model=self.model,
            input=self.get_context_window(),
        )
        self.messages.append((MessageSource.AGENT, response.output_text))
        return response.output_text

    def get_context_window(self) -> str:
        return "\n".join([
            f"<{source.value}>{content}</{source.value}>"
            for source, content in self.messages
        ])
