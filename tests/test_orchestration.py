from collections.abc import Callable, Generator
from pathlib import Path
from threading import Event
from typing import cast

import pytest
from yaml import safe_load

from jri.core import paths
from jri.core.ai import ToolCallFinished, ToolCallStarted, architect, functional_analyst, specs_generation
from jri.core.exceptions import RepositoryStateError, SpecsError
from jri.core.notes import Notebook
from jri.core.specs import ACCEPTANCE_TRAILER
from tests.conftest import CreateRepository, RunGit
from tests.doubles.openai import FakeClient, call, partial_reply, reply, response, stopped_stream, streamed_reply
from tests.doubles.settings import build_settings
from tests.doubles.workspace import install_workspace

type Result = functional_analyst.Ambiguities | str | None
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
ARCHITECTURE_UPDATE = """\
diff --git a/architecture/design.md b/architecture/design.md
--- a/architecture/design.md
+++ b/architecture/design.md
@@ -1 +1,2 @@
 # Design
+Add a total accumulator.
"""


def build_workspace(path: Path, create_repository: CreateRepository) -> None:
    create_repository(path)
    install_workspace(path)


def generate(
    client: FakeClient, stop_at: Row | None = None, cancelled: Event | None = None
) -> tuple[list[Row], Result]:
    captured: list[Result] = []
    cancelled = cancelled or Event()
    settings = build_settings(client)

    def drive() -> Generator[Row]:
        captured.append((yield from specs_generation.generate(settings, cancelled)))

    rows: list[Row] = []
    # A stop reaches the run while the row naming the step it
    # interrupts is the one on screen.
    for row in drive():
        rows.append(row)
        if row == stop_at:
            cancelled.set()
    return rows, captured[0]


def commit_specs() -> None:
    client = FakeClient([streamed_reply("Repository report")], parsed=[written_specs(), designed_architecture()])
    _, commit = generate(client)
    assert isinstance(commit, str)


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


def test_writes_specifications_from_the_topics_the_user_kept(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    notebook = Notebook(tmp_path / paths.NOTEBOOK_FILE)
    notebook.add(["Ship a web app."], "t1")
    discarded = notebook.add_topic("Discarded")
    notebook.add(["Build a rocket instead."], discarded.id)
    notebook.update_topic(discarded.id, "trashed")
    client = FakeClient([streamed_reply("Repository report")], parsed=[written_specs(), designed_architecture()])

    generate(client)

    prompts = read_prompts(client)
    assert any("Ship a web app." in prompt for prompt in prompts)
    assert not any("Build a rocket instead." in prompt for prompt in prompts)


def test_writes_specifications_without_diffing_the_topics_the_user_threw_away(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    notebook = Notebook(tmp_path / paths.NOTEBOOK_FILE)
    notebook.add(["Ship a web app."], "t1")
    discarded = notebook.add_topic("Discarded")
    notebook.add(["Build a rocket instead."], discarded.id)
    notebook.update_topic(discarded.id, "trashed")
    commit_specs()
    Notebook(tmp_path / paths.NOTEBOOK_FILE).add(["Export the data as CSV."], "t1")
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[
            functional_analyst.Output(
                result=functional_analyst.Patch(outcome="specification_patch", patch=FUNCTIONAL_UPDATE)
            ),
            architect.Output(result=architect.Patch(outcome="architecture_patch", patch=ARCHITECTURE_UPDATE)),
        ],
    )

    _, result = generate(client)

    assert isinstance(result, str)
    diff = next(prompt for prompt in read_prompts(client) if "Notebook diff from accepted baseline:" in prompt)
    assert "Export the data as CSV." in diff
    assert "Build a rocket instead." not in diff


def test_writes_specifications_against_an_accepted_notebook_it_cannot_read(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    build_workspace(tmp_path, create_repository)
    (tmp_path / paths.NOTEBOOK_FILE).write_text("nonsense\n")
    run_git(tmp_path, "add", "--force", paths.NOTEBOOK_FILE)
    run_git(tmp_path, "commit", "-qm", f"jri: update specifications\n\n{ACCEPTANCE_TRAILER}")
    (tmp_path / paths.NOTEBOOK_FILE).unlink()
    Notebook(tmp_path / paths.NOTEBOOK_FILE).add(["Ship a web app."], "t1")
    client = FakeClient([streamed_reply("Repository report")], parsed=[written_specs(), designed_architecture()])

    _, result = generate(client)

    assert isinstance(result, str)
    prompts = read_prompts(client)
    assert any("Ship a web app." in prompt for prompt in prompts)
    assert not any("nonsense" in prompt for prompt in prompts)
    diff = next(prompt for prompt in prompts if "Notebook diff from accepted baseline:" in prompt)
    # A baseline JRI cannot read is no baseline: the whole notebook
    # arrives as additions, the state a first generation reports,
    # rather than as the nothing-changed an unreadable one would.
    assert "@@ -0,0 +1," in diff
    assert "+    n1: Ship a web app." in diff


@pytest.mark.parametrize(
    "stop_at",
    [
        ToolCallStarted("functional", "Writing functional specifications from your project notes", "✍️"),
        ToolCallStarted("architecture", "Designing the project architecture", "📐"),
        ToolCallFinished("architecture", "Designed the project architecture", "done"),
    ],
    ids=["writing", "designing", "designed"],
)
def test_stops_a_run_without_touching_the_project(
    stop_at: Row, tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    build_workspace(tmp_path, create_repository)
    head = run_git(tmp_path, "rev-parse", "HEAD")
    client = FakeClient([streamed_reply("Repository report")], parsed=[written_specs(), designed_architecture()])

    rows, result = generate(client, stop_at)

    assert result is None
    # The run says nothing more, leaving the row the stop landed on
    # for the turn to close.
    assert rows[-1] == stop_at
    assert run_git(tmp_path, "rev-parse", "HEAD") == head
    assert not (tmp_path / paths.SPECS_DIR).exists()
    # Nothing staged and no worktree left behind: a stop that left
    # either would be worse than never stopping.
    assert not run_git(tmp_path, "diff", "--cached")
    assert len(run_git(tmp_path, "worktree", "list").splitlines()) == 1


@pytest.mark.parametrize(
    "queue_responses",
    [
        lambda cancelled: [stopped_stream(cancelled)],
        lambda cancelled: [
            functional_analyst.Output(
                result=functional_analyst.Patch(outcome="specification_patch", patch=FUNCTIONAL_UPDATE)
            ),
            stopped_stream(cancelled),
        ],
        lambda cancelled: [written_specs(), stopped_stream(cancelled)],
        lambda cancelled: [
            written_specs(),
            architect.Output(result=architect.Patch(outcome="architecture_patch", patch=ARCHITECTURE_UPDATE)),
            stopped_stream(cancelled),
        ],
    ],
    ids=["writing", "repairing-the-specifications", "designing", "repairing-the-architecture"],
)
def test_stops_a_run_while_a_model_is_still_answering(
    queue_responses: Callable[[Event], list[object]],
    tmp_path: Path,
    create_repository: CreateRepository,
    run_git: RunGit,
) -> None:
    build_workspace(tmp_path, create_repository)
    head = run_git(tmp_path, "rev-parse", "HEAD")
    cancelled = Event()
    client = FakeClient([streamed_reply("Repository report")], parsed=queue_responses(cancelled))

    _, result = generate(client, cancelled=cancelled)

    # A stop mid-answer ends the run where a stop between two steps
    # does, rather than after the minutes that answer had left.
    assert result is None
    assert run_git(tmp_path, "rev-parse", "HEAD") == head
    assert not (tmp_path / paths.SPECS_DIR).exists()
    assert not run_git(tmp_path, "diff", "--cached")
    assert len(run_git(tmp_path, "worktree", "list").splitlines()) == 1


def test_stops_the_repository_study_without_calling_it_a_failure(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    # The only round the study is served, so a run that carried on
    # would answer this call and then ask for a round nobody queued.
    client = FakeClient(
        [response(call("read", "read_files", paths=["README.md"]))], parsed=[written_specs(), designed_architecture()]
    )

    rows, result = generate(client, ToolCallStarted("explorer", "Studying your existing project", "🔎"))

    assert result is None
    assert read_rows(rows)[-1] == ("ToolCallStarted", "explorer", "Studying your existing project")
    assert architect.Output not in [options.get("text_format") for options in client.responses.options]


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
        ("ToolCallStarted", "commit", "Saving the specifications to your project"),
        ("ToolCallFinished", "commit", "Saved the specifications to your project"),
    ]
    assert isinstance(result, str)


def test_leaves_the_saving_row_open_when_the_project_blocks_the_commit(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient([streamed_reply("Repository report")], parsed=[written_specs(), designed_architecture()])
    rows: list[Row] = []

    def block_the_project_once_the_design_lands() -> None:
        for row in specs_generation.generate(build_settings(client)):
            rows.append(row)
            if isinstance(row, ToolCallFinished) and row.call_id == "architecture":
                stray = tmp_path / paths.FUNCTIONAL_SPECS_DIR / "stray.md"
                stray.parent.mkdir(parents=True)
                stray.write_text("blocked")

    with pytest.raises(RepositoryStateError, match=r"stray\.md"):
        block_the_project_once_the_design_lands()

    assert read_rows(rows)[-2:] == [
        ("ToolCallFinished", "architecture", "Designed the project architecture"),
        ("ToolCallStarted", "commit", "Saving the specifications to your project"),
    ]


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
    assert "File: functional/behavior.md" in revision
    assert "# Behavior" in revision
    assert "Architect feedback:\n  - Undefined totals.\n  - Unclear export." in revision


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
    assert report.endswith("Repository analysis report:\n```\nFinal report\n```")


def test_keeps_what_the_repository_study_writes_out_of_the_project(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient(
        [response(call("shell", "run_shell", command="touch uv.lock")), streamed_reply("Repository report")],
        parsed=[written_specs(), designed_architecture()],
    )

    _, result = generate(client)

    assert isinstance(result, str)
    assert not (tmp_path / "uv.lock").exists()
    assert not run_git(tmp_path, "status", "--short")


def test_studies_the_project_as_it_stands_on_disk(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    build_workspace(tmp_path, create_repository)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n")
    run_git(tmp_path, "add", "src/app.py")
    run_git(tmp_path, "commit", "-qm", "feat: add an application")
    client = FakeClient(
        [response(call("read", "read_files", paths=[paths.NOTEBOOK_FILE])), streamed_reply("Repository report")],
        parsed=[written_specs(), designed_architecture()],
    )

    generate(client)

    tree = next(prompt for prompt in read_prompts(client) if "Tracked repository tree:" in prompt)
    assert "src/app.py" in tree
    assert paths.NOTEBOOK_FILE in tree
    instructions = next(prompt for prompt in read_prompts(client) if "Role: Explorer." in prompt)
    directory = Path(instructions.split("Working directory: ")[1].splitlines()[0])
    assert not directory.is_relative_to(tmp_path.resolve())
    assert str(directory / paths.NOTEBOOK_FILE) in str(client.responses.inputs)


def test_studies_a_project_whose_only_commit_holds_no_project_files(tmp_path: Path, run_git: RunGit) -> None:
    run_git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("# Project\n")
    install_workspace(tmp_path)
    run_git(tmp_path, "add", paths.WORKSPACE_DIR)
    run_git(tmp_path, "commit", "-qm", "jri: update specifications")
    client = FakeClient([streamed_reply("Repository report")], parsed=[written_specs(), designed_architecture()])

    generate(client)

    tree = next(prompt for prompt in read_prompts(client) if "Tracked repository tree:" in prompt)
    assert "README.md" in tree


def test_reports_a_file_name_holding_a_newline_as_one_tracked_path(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    (tmp_path / "notes.md\nsecret.md").write_text("# Notes\n")
    client = FakeClient([streamed_reply("Repository report")], parsed=[written_specs(), designed_architecture()])

    generate(client)

    tree = next(prompt for prompt in read_prompts(client) if "Tracked repository tree:" in prompt)
    listed = safe_load(tree.partition("Tracked repository tree:\n")[2].partition("\n\nRepository analysis report:")[0])
    assert "notes.md\nsecret.md" in listed
    assert "secret.md" not in listed


def test_refuses_an_empty_repository_report(tmp_path: Path, create_repository: CreateRepository) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient([response(reply(""))], parsed=[written_specs()])

    with pytest.raises(SpecsError, match="produced no report"):
        generate(client)


def test_refuses_a_patch_that_deletes_every_functional_specification(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    commit_specs()
    client = FakeClient(
        [],
        parsed=[
            functional_analyst.Output(
                result=functional_analyst.Patch(outcome="specification_patch", patch=FUNCTIONAL_DELETION_PATCH)
            )
        ],
    )

    with pytest.raises(SpecsError, match="Functional specifications cannot be empty"):
        generate(client)


def test_refuses_a_patch_that_deletes_every_architecture_specification(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    commit_specs()
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
        generate(client)


def test_sends_a_rejected_functional_patch_back_to_the_analyst(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[
            functional_analyst.Output(
                result=functional_analyst.Patch(outcome="specification_patch", patch=FUNCTIONAL_UPDATE)
            ),
            functional_analyst.Patch(outcome="specification_patch", patch=FUNCTIONAL_PATCH),
            designed_architecture(),
        ],
    )

    _, result = generate(client)

    assert isinstance(result, str)
    repair = next(prompt for prompt in read_prompts(client) if "Rejected patch:" in prompt)
    assert FUNCTIONAL_UPDATE in repair
    assert "Git error:" in repair


def test_sends_a_rejected_architecture_patch_back_to_the_architect(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[
            written_specs(),
            architect.Output(result=architect.Patch(outcome="architecture_patch", patch=ARCHITECTURE_UPDATE)),
            architect.Patch(outcome="architecture_patch", patch=ARCHITECTURE_PATCH),
        ],
    )

    _, result = generate(client)

    assert isinstance(result, str)
    assert (tmp_path / paths.ARCHITECTURE_SPECS_DIR / "design.md").read_text() == "# Design\n"


def test_refuses_a_repaired_patch_that_leaves_its_root(tmp_path: Path, create_repository: CreateRepository) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient(
        [],
        parsed=[
            functional_analyst.Output(
                result=functional_analyst.Patch(outcome="specification_patch", patch=FUNCTIONAL_UPDATE)
            ),
            functional_analyst.Patch(outcome="specification_patch", patch=ARCHITECTURE_PATCH),
        ],
    )

    with pytest.raises(SpecsError, match=r"cannot change `architecture/design\.md`"):
        generate(client)


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
