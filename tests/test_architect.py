from collections.abc import Generator
from threading import Event
from typing import cast

from jri.core.ai import ReasoningDelta, architect
from tests.doubles.openai import FakeClient, reply, response, thought
from tests.doubles.settings import build_settings

CONTEXT = architect.Input(
    functional_specs="File: functional/behavior.md",
    current_architecture="(empty)",
    tracked_repository_tree=["README.md"],
    explorer_report="One Python package.",
)
ARCHITECTURE = architect.Architecture(
    outcome="architecture",
    files=[architect.File(path="architecture/design.md", content="# Design\n")],
    deleted_paths=[],
)


def build_architect(client: FakeClient) -> architect.Architect:
    return architect.Architect(build_settings(client))


def drain(call: Generator[ReasoningDelta, None, object]) -> tuple[list[ReasoningDelta], object]:
    thoughts: list[ReasoningDelta] = []
    while True:
        try:
            thoughts.append(next(call))
        except StopIteration as stop:
            return thoughts, stop.value


def test_designs_the_architecture_files() -> None:
    client = FakeClient([], parsed=[architect.Output(result=ARCHITECTURE)])

    assert drain(build_architect(client).design(CONTEXT, Event()))[1] == ARCHITECTURE


# A design pass can take several minutes.
# Model reasoning belongs to the open design round.
# It does not belong to the returned architecture.
def test_streams_the_thinking_of_a_pass_before_the_architecture_it_designed() -> None:
    client = FakeClient(
        [],
        parsed=[
            [thought("Weighing the layers."), *response(reply(architect.Output(result=ARCHITECTURE).model_dump_json()))]
        ],
    )

    thoughts, result = drain(build_architect(client).design(CONTEXT, Event()))

    assert thoughts == [ReasoningDelta("Weighing the layers.")]
    assert result == ARCHITECTURE


def test_reports_functional_issues_instead_of_an_architecture() -> None:
    issues = architect.Issues(outcome="functional_specification_issues", issues=["Undefined totals."])
    client = FakeClient([], parsed=[architect.Output(result=issues)])

    result = drain(build_architect(client).design(CONTEXT, Event()))[1]

    assert result == issues
    assert client.responses.options[-1]["text_format"] is architect.Output


def test_leaves_the_final_pass_no_way_to_report_issues() -> None:
    client = FakeClient([], parsed=[ARCHITECTURE])

    assert drain(build_architect(client).finish(CONTEXT, Event()))[1] == ARCHITECTURE
    assert client.responses.options[-1]["text_format"] is architect.Architecture
    prompt = cast("list[dict[str, object]]", client.responses.inputs[-1])[0]["content"]
    assert str(prompt).endswith(architect.Architect.FINAL_PROMPT)
