from collections.abc import Generator
from pathlib import Path
from threading import Event
from typing import cast

from jri.core.ai import ReasoningDelta, ToolCallFinished, ToolCallStarted, architect
from jri.lib import git
from tests.conftest import CreateRepository
from tests.doubles.openai import FakeClient, reply, response, thought
from tests.doubles.settings import build_settings

CONTEXT = architect.Input(
    functional_specs_index="functional/behavior.md: How the product behaves.",
    current_architecture_index="(empty)",
    tracked_repository_tree=["README.md"],
    explorer_report="One Python package.",
)
ARCHITECTURE = architect.Architecture(
    outcome="architecture",
    files=[architect.File(path="architecture/design.md", content="# Design\n", summary="How the system is built.")],
    deleted_paths=[],
)


def build_architect(client: FakeClient, repository_path: Path, *, final: bool = False) -> architect.Architect:
    return architect.Architect(build_settings(client), git.Repository(repository_path), final=final)


def drain(
    call: Generator["ReasoningDelta | ToolCallStarted | ToolCallFinished", None, object],
) -> tuple[list[ReasoningDelta], object]:
    thoughts: list[ReasoningDelta] = []
    while True:
        try:
            event = next(call)
            if isinstance(event, ReasoningDelta):
                thoughts.append(event)
        except StopIteration as stop:
            return thoughts, stop.value


def test_designs_the_architecture_files(tmp_path: Path, create_repository: CreateRepository) -> None:
    create_repository(tmp_path)
    client = FakeClient([], parsed=[architect.Output(result=ARCHITECTURE)])

    assert drain(build_architect(client, tmp_path).design(CONTEXT, Event()))[1] == ARCHITECTURE


# A design pass can take several minutes.
# Model reasoning belongs to the open design round.
# It does not belong to the returned architecture.
def test_streams_the_thinking_of_a_pass_before_the_architecture_it_designed(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    create_repository(tmp_path)
    client = FakeClient(
        [],
        parsed=[
            [thought("Weighing the layers."), *response(reply(architect.Output(result=ARCHITECTURE).model_dump_json()))]
        ],
    )

    thoughts, result = drain(build_architect(client, tmp_path).design(CONTEXT, Event()))

    assert thoughts == [ReasoningDelta("Weighing the layers.")]
    assert result == ARCHITECTURE


def test_reports_functional_issues_instead_of_an_architecture(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    create_repository(tmp_path)
    issues = architect.Issues(outcome="functional_specification_issues", issues=["Undefined totals."])
    client = FakeClient([], parsed=[architect.Output(result=issues)])

    result = drain(build_architect(client, tmp_path).design(CONTEXT, Event()))[1]

    assert result == issues
    assert client.responses.options[-1]["text_format"] is architect.Output


def test_leaves_the_final_pass_no_way_to_report_issues(tmp_path: Path, create_repository: CreateRepository) -> None:
    create_repository(tmp_path)
    client = FakeClient([], parsed=[ARCHITECTURE])

    assert drain(build_architect(client, tmp_path, final=True).design(CONTEXT, Event()))[1] == ARCHITECTURE
    assert client.responses.options[-1]["text_format"] is architect.Architecture
    prompt = cast("list[dict[str, object]]", client.responses.inputs[-1])[0]["content"]
    assert str(prompt).startswith(architect.Architect.FINAL_PROMPT)
