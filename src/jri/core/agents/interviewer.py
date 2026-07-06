from jri.core.settings import Settings

from .explorer import Explorer
from .shared import Agent, TextDelta, ToolCallStarted, tool


class Interviewer(Agent):
    FIRST_MESSAGE: str = "What do you want to build?"

    def __init__(self, settings: Settings) -> None:
        self.settings: Settings = settings
        super().__init__(
            client=self.settings.llm_client,
            model=self.settings.interviewer_model,
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
        (
            "Gather context through a natural language query, "
            "including anything from the web or this computer."
        ),
    )
    def explore(self, query: str) -> str:
        explorer = Explorer(self.settings)
        latest_output: list[str] = []

        for event in explorer.send_message(query):
            if isinstance(event, TextDelta):
                latest_output.append(event.text)
                continue

            if isinstance(event, ToolCallStarted):
                latest_output = []

        return "".join(latest_output)
