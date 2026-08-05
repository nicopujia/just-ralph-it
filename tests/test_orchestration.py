from collections.abc import Generator
from pathlib import Path
from typing import cast

import pytest

from jri.core import paths
from jri.core.ai import ToolCallFinished, ToolCallStarted, architect, functional_analyst, specs_generation
from jri.core.exceptions import SpecsError
from tests.conftest import CreateRepository, RunGit
from tests.doubles.openai import FakeClient, call, partial_reply, reply, response, streamed_reply
from tests.doubles.settings import build_settings
from tests.doubles.workspace import install_workspace

type Result = functional_analyst.Ambiguities | str
type Row = ToolCallStarted | ToolCallFinished

ARCHITECTURE_PATCH = """\
diff --git a/architecture/design.md b/architecture/design.md
new file mode 100644
--- /dev/null
+++ b/architecture/design.md
@@ -0,0 +1 @@
+# Design
"""
FUNCTIONAL_PATCH = """\
diff --git a/functional/behavior.md b/functional/behavior.md
new file mode 100644
--- /dev/null
+++ b/functional/behavior.md
@@ -0,0 +1 @@
+# Behavior
"""
FUNCTIONAL_UPDATE = """\
diff --git a/functional/behavior.md b/functional/behavior.md
--- a/functional/behavior.md
+++ b/functional/behavior.md
@@ -1 +1,2 @@
 # Behavior
+Total output is supported.
"""
FUNCTIONAL_DELETION_PATCH = """\
diff --git a/functional/behavior.md b/functional/behavior.md
deleted file mode 100644
--- a/functional/behavior.md
+++ /dev/null
@@ -1 +0,0 @@
-# Behavior
"""
ARCHITECTURE_DELETION_PATCH = """\
diff --git a/architecture/design.md b/architecture/design.md
deleted file mode 100644
--- a/architecture/design.md
+++ /dev/null
@@ -1 +0,0 @@
-# Design
"""


def build_workspace(path: Path, create_repository: CreateRepository) -> None:
    create_repository(path)
    install_workspace(path)


def generate(client: FakeClient, active_commit: str | None = None) -> tuple[list[Row], Result]:
    captured: list[Result] = []
    settings = build_settings(client)

    def drive() -> Generator[Row]:
        captured.append((yield from specs_generation.generate(settings, active_commit)))

    return list(drive()), captured[0]


def commit_specs() -> str:
    client = FakeClient([streamed_reply("Repository report")], parsed=[written_specs(), designed_architecture()])
    _, commit = generate(client)
    assert isinstance(commit, str)
    return commit


def written_specs() -> functional_analyst.Output:
    return functional_analyst.Output(
        result=functional_analyst.Patch(outcome="specification_patch", patch=FUNCTIONAL_PATCH)
    )


def designed_architecture() -> architect.Output:
    return architect.Output(result=architect.Patch(outcome="architecture_patch", patch=ARCHITECTURE_PATCH))


def reported_issues(*issues: str) -> architect.Output:
    return architect.Output(result=architect.Issues(outcome="functional_specification_issues", issues=list(issues)))


def read_prompts(client: FakeClient) -> list[str]:
    return [
        str(message.get("content", ""))
        for context in client.responses.inputs
        for message in cast("list[dict[str, object]]", context)
    ]


def read_rows(rows: list[Row]) -> list[tuple[str, str, str]]:
    return [(type(row).__name__, row.call_id, row.label) for row in rows]


def test_returns_ambiguities_without_committing(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    build_workspace(tmp_path, create_repository)
    head = run_git(tmp_path, "rev-parse", "HEAD")
    ambiguities = functional_analyst.Ambiguities(outcome="ambiguities", ambiguities=["JSON or plain text?"])
    client = FakeClient([], parsed=[functional_analyst.Output(result=ambiguities)])

    rows, result = generate(client)

    assert result == ambiguities
    assert read_rows(rows) == [
        ("ToolCallStarted", "functional", "Writing functional specifications from your project notes"),
        ("ToolCallFinished", "functional", "Found project details to clarify"),
    ]
    assert run_git(tmp_path, "rev-parse", "HEAD") == head
    assert not (tmp_path / paths.SPECS_DIR).exists()


def test_reports_one_row_per_polishing_round(tmp_path: Path, create_repository: CreateRepository) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[
            written_specs(),
            reported_issues("Undefined totals.", "Unclear export.", "Missing errors."),
            written_specs(),
            reported_issues("Unclear export."),
            written_specs(),
            reported_issues("Missing errors.", "Undefined limits."),
            written_specs(),
            designed_architecture(),
        ],
    )

    rows, result = generate(client)

    assert read_rows(rows) == [
        ("ToolCallStarted", "functional", "Writing functional specifications from your project notes"),
        ("ToolCallFinished", "functional", "Wrote functional specifications from your project notes"),
        ("ToolCallStarted", "explorer", "Studying your existing project"),
        ("ToolCallFinished", "explorer", "Studied your existing project"),
        ("ToolCallStarted", "architecture", "Designing the project architecture"),
        ("ToolCallFinished", "architecture", "Drafted the project architecture"),
        ("ToolCallStarted", "polish-1", "3 issues found. Polishing... (round 1)"),
        ("ToolCallFinished", "polish-1", "3 issues found. Polishing... (round 1)"),
        ("ToolCallStarted", "polish-2", "1 issues found. Polishing... (round 2)"),
        ("ToolCallFinished", "polish-2", "1 issues found. Polishing... (round 2)"),
        ("ToolCallStarted", "polish-3", "2 issues found. Polishing... (round 3)"),
        ("ToolCallFinished", "polish-3", "2 issues found. Polishing... (round 3)"),
    ]
    assert isinstance(result, str)


def test_finishes_the_open_polishing_round_when_ambiguities_appear(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[
            written_specs(),
            reported_issues("Unclear export.", "Missing errors."),
            functional_analyst.Output(
                result=functional_analyst.Ambiguities(outcome="ambiguities", ambiguities=["JSON or plain text?"])
            ),
        ],
    )

    rows, _ = generate(client)

    assert read_rows(rows)[-2:] == [
        ("ToolCallStarted", "polish-1", "2 issues found. Polishing... (round 1)"),
        ("ToolCallFinished", "polish-1", "Found project details to clarify"),
    ]
    assert [row.call_id for row in rows if isinstance(row, ToolCallStarted)] == [
        row.call_id for row in rows if isinstance(row, ToolCallFinished)
    ]


def test_sends_the_architect_issues_back_to_the_functional_analyst(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[
            written_specs(),
            reported_issues("Undefined totals.", "Unclear export."),
            written_specs(),
            designed_architecture(),
        ],
    )

    generate(client)

    revision = next(prompt for prompt in read_prompts(client) if "Rejected functional draft:" in prompt)
    assert "File: functional/behavior.md\n\n# Behavior" in revision
    assert "Architect feedback:\n- Undefined totals.\n- Unclear export." in revision


def test_asks_the_architect_to_finish_on_the_last_cycle(tmp_path: Path, create_repository: CreateRepository) -> None:
    build_workspace(tmp_path, create_repository)
    parsed: list[object] = [
        item
        for _ in range(specs_generation.MAX_CYCLES - 1)
        for item in (written_specs(), reported_issues("Unclear export."))
    ]
    parsed.extend([written_specs(), architect.Patch(outcome="architecture_patch", patch=ARCHITECTURE_PATCH)])
    client = FakeClient([streamed_reply("Repository report")], parsed=parsed)

    rows, result = generate(client)

    assert client.responses.options[-1]["text_format"] is architect.Patch
    assert len([row for row in rows if isinstance(row, ToolCallStarted) and "polish" in row.call_id]) == (
        specs_generation.MAX_CYCLES - 1
    )
    assert isinstance(result, str)


def test_reports_only_the_explorer_text_that_follows_its_last_tool_call(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient(
        [
            [*partial_reply("Draft notes"), *response(call("search", "search_web", query="ralph"))],
            streamed_reply("Final report"),
        ],
        parsed=[written_specs(), designed_architecture()],
    )

    generate(client)

    report = next(prompt for prompt in read_prompts(client) if "Repository analysis report:" in prompt)
    assert report.endswith("Repository analysis report:\nFinal report")


def test_refuses_an_empty_repository_report(tmp_path: Path, create_repository: CreateRepository) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient([response(reply(""))], parsed=[written_specs()])

    with pytest.raises(SpecsError, match="produced no report"):
        generate(client)


def test_refuses_a_patch_that_deletes_every_functional_specification(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    commit = commit_specs()
    client = FakeClient(
        [],
        parsed=[
            functional_analyst.Output(
                result=functional_analyst.Patch(outcome="specification_patch", patch=FUNCTIONAL_DELETION_PATCH)
            )
        ],
    )

    with pytest.raises(SpecsError, match="Functional specifications cannot be empty"):
        generate(client, commit)


def test_refuses_a_patch_that_deletes_every_architecture_specification(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    commit = commit_specs()
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[
            functional_analyst.Output(
                result=functional_analyst.Patch(outcome="specification_patch", patch=FUNCTIONAL_UPDATE)
            ),
            architect.Output(result=architect.Patch(outcome="architecture_patch", patch=ARCHITECTURE_DELETION_PATCH)),
        ],
    )

    with pytest.raises(SpecsError, match="Architecture specifications cannot be empty"):
        generate(client, commit)


def test_refuses_an_architecture_patch_that_leaves_its_root(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[
            written_specs(),
            architect.Output(result=architect.Patch(outcome="architecture_patch", patch=FUNCTIONAL_PATCH)),
        ],
    )

    with pytest.raises(SpecsError, match=r"cannot change `functional/behavior\.md`"):
        generate(client)
