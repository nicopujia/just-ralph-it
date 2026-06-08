from abc import ABC, abstractmethod
from collections.abc import Generator, Iterable

from openai import OpenAI
from openai.types.responses.response_input_param import ResponseInputParam
from openai.types.responses.tool_param import ToolParam


class Agent(ABC):
    def __init__(
        self,
        *,
        client: OpenAI,
        model: str,
        tools: Iterable[ToolParam],
        initial_context_window: ResponseInputParam | None = None,
    ) -> None:
        self.client: OpenAI = client
        self.model: str = model
        self.context_window: ResponseInputParam = initial_context_window or []
        self.tools: Iterable[ToolParam] = tools

    @abstractmethod
    def send_message(self, message: str) -> Generator[str] | str:
        raise NotImplementedError
