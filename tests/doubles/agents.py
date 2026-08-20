from collections.abc import Generator

from jri.core.ai import ReasoningDelta, ToolCallFinished, ToolCallStarted

type Progress = ReasoningDelta | ToolCallStarted | ToolCallFinished


# An agent streams its progress while it works and returns its result at the end. A caller that wants either one
# must read the whole stream, so this reads the reasoning and returns it beside the result.
def drain[Result](progress: Generator[Progress, None, Result]) -> tuple[list[ReasoningDelta], Result]:
    thoughts: list[ReasoningDelta] = []
    while True:
        try:
            event = next(progress)
            if isinstance(event, ReasoningDelta):
                thoughts.append(event)
        except StopIteration as stop:
            return thoughts, stop.value
