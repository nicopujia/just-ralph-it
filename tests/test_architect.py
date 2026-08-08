from threading import Event
from typing import cast

from jri.core.ai import architect
from tests.doubles.openai import FakeClient
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


def test_designs_the_architecture_files() -> None:
    client = FakeClient([], parsed=[architect.Output(result=ARCHITECTURE)])

    assert build_architect(client).design(CONTEXT, Event()) == ARCHITECTURE


def test_reports_functional_issues_instead_of_an_architecture() -> None:
    issues = architect.Issues(outcome="functional_specification_issues", issues=["Undefined totals."])
    client = FakeClient([], parsed=[architect.Output(result=issues)])

    result = build_architect(client).design(CONTEXT, Event())

    assert result == issues
    assert client.responses.options[-1]["text_format"] is architect.Output


def test_leaves_the_final_pass_no_way_to_report_issues() -> None:
    client = FakeClient([], parsed=[ARCHITECTURE])

    assert build_architect(client).finish(CONTEXT, Event()) == ARCHITECTURE
    assert client.responses.options[-1]["text_format"] is architect.Architecture
    prompt = cast("list[dict[str, object]]", client.responses.inputs[-1])[0]["content"]
    assert str(prompt).endswith(architect.Architect.FINAL_PROMPT)
