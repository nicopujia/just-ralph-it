from collections.abc import Generator

from jri.core.ai import Exploration, ReasoningDelta, ToolCallFinished, ToolCallStarted
from tests.doubles.openai import reply, response, thought

type Progress = ReasoningDelta | ToolCallStarted | ToolCallFinished

EXPLORATION_SUMMARY = "The project, in two lines."


# This exploration answers its query in one segment. It leaves no work for a second segment.
# A segment that sends its reasoning needs a round of its own, because the reasoning comes before the result.
def explored(report: str = "Repository report", thinking: str = "") -> list[object]:
    exploration = Exploration(report=report, summary=EXPLORATION_SUMMARY, remaining="")
    if not thinking:
        return [exploration]
    return [[thought(thinking), *response(reply(exploration.model_dump_json()))]]


# An agent streams its progress while it works. It returns its result at the end. A caller that wants one of
# these two must read the full stream. This function reads the reasoning and returns it with the result.
def drain[Result](progress: Generator[Progress, None, Result]) -> tuple[list[ReasoningDelta], Result]:
    thoughts: list[ReasoningDelta] = []
    while True:
        try:
            event = next(progress)
            if isinstance(event, ReasoningDelta):
                thoughts.append(event)
        except StopIteration as stop:
            return thoughts, stop.value
