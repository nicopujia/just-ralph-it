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
            reasoning_effort=settings.interviewer_reasoning_effort,
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
        self.active_explorer: Explorer | None = None

    def cancel(self) -> None:
        """Cancel the active response and any nested exploration."""

        super().cancel()
        if explorer := self.active_explorer:
            explorer.cancel()

    def close(self) -> None:
        """Close active response and nested exploration resources."""

        super().close()
        if explorer := self.active_explorer:
            explorer.close()

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
        explorer = Explorer(self.settings, self.cancellation_event)
        self.active_explorer = explorer
        for event in explorer.send_message(query):
            match event:
                case ToolCallStarted():
                    latest_output.clear()
                    yield event
                case ToolCallFinished():
                    yield event
                case TextDelta():
                    latest_output.append(event.text)
        self.active_explorer = None
        yield ToolOutput("".join(latest_output))
