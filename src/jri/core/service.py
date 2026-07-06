from collections.abc import Generator

from .agents import Interviewer
from .agents.shared import ChatEvent
from .settings import Settings, get_settings


class Service:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings: Settings = settings or get_settings()
        self.interviewer: Interviewer = Interviewer(self.settings)

    def chat(self, message: str) -> Generator[ChatEvent]:
        yield from self.interviewer.send_message(message)
