from pathlib import Path
from typing import cast

from jri.core.ai import architect
from tests.doubles.openai import FakeClient
from tests.doubles.settings import build_settings

CONTEXT = architect.Input(
    functional_specs="File: functional/behavior.md",
    accepted_architecture="(empty)",
    tracked_tree="README.md",
    explorer_report="One Python package.",
)
PATCH = architect.Patch(outcome="architecture_patch", patch="diff --git a/architecture/x.md b/architecture/x.md\n")


def build_architect(path: Path, client: FakeClient) -> architect.Architect:
    return architect.Architect(build_settings(path, client))


def test_designs_an_architecture_patch(tmp_path: Path) -> None:
    client = FakeClient([], parsed=[architect.Output(result=PATCH)])

    assert build_architect(tmp_path, client).design(CONTEXT) == PATCH


def test_reports_functional_issues_instead_of_a_patch(tmp_path: Path) -> None:
    issues = architect.Issues(outcome="functional_specification_issues", issues=["Undefined totals."])
    client = FakeClient([], parsed=[architect.Output(result=issues)])

    result = build_architect(tmp_path, client).design(CONTEXT)

    assert result == issues
    assert client.responses.options[-1]["text_format"] is architect.Output


def test_leaves_the_final_pass_no_way_to_report_issues(tmp_path: Path) -> None:
    client = FakeClient([], parsed=[PATCH])

    assert build_architect(tmp_path, client).finish(CONTEXT) == PATCH
    assert client.responses.options[-1]["text_format"] is architect.Patch
    prompt = cast("list[dict[str, object]]", client.responses.inputs[-1])[0]["content"]
    assert str(prompt).endswith(architect.Architect.FINAL_PROMPT)
