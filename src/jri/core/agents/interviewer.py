from collections.abc import Generator

from jri.core.settings import Settings

from .explorer import Explorer
from .shared import Agent, TextDelta, ToolCallFinished, ToolCallStarted, ToolOutput, tool


class Interviewer(Agent):
    """Agent that interviews the user to extract a project idea."""

    FIRST_MESSAGE = "What do you want to build?"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        super().__init__(
            client=settings.llm_client,
            model=settings.interviewer_model,
            sys_prompt="""
                You are the Interviewer of the Just Ralph It (JRI) system,
                which is a tool to build any software project.

                Your task is extract the full project idea that the user wants
                to build out of their mind.

                Rules:
                - Prefer answering questions with `explore` tool when possible.
            """,
            initial_ctx=[{"role": "assistant", "content": self.FIRST_MESSAGE}],
        )

    @tool(
        "Gather context through a natural language query, including anything from the web or this computer.",
        started_label='Exploring "{query}"',
        finished_label='Explored "{query}"',
        symbol="🔍",
    )
    def explore(self, query: str) -> Generator[ToolCallStarted | ToolCallFinished | ToolOutput]:
        """Gather extra context for the user request.

        Yields:
            Explorer tool events followed by its final text output.
        """

        latest_output: list[str] = []
        for event in Explorer(self.settings).send_message(query):
            match event:
                case ToolCallStarted():
                    latest_output.clear()
                    yield event
                case ToolCallFinished():
                    yield event
                case TextDelta():
                    latest_output.append(event.text)
        yield ToolOutput("".join(latest_output))
