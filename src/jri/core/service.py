from collections.abc import Generator

from openai import OpenAI

from .agents import Interviewer
from .settings import Settings, get_settings


class Service:
    def __init__(
        self,
        client: OpenAI | None = None,
        interviewer: Interviewer | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings: Settings = settings or get_settings()
        self.client: OpenAI = client or OpenAI(
            base_url=self.settings.llm_provider_base_url,
            api_key=self.settings.llm_api_key,
        )
        self.interviewer: Interviewer = interviewer or Interviewer(
            client=self.client,
            model=self.settings.interviewer_model,
        )

    def chat(self, message: str) -> Generator[str]:
        yield from self.interviewer.send_message(message)
