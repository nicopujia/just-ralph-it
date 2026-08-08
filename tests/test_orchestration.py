from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from threading import Event
from typing import cast

import pytest
from yaml import safe_load

from jri.core import paths
from jri.core.ai import (
    ReasoningDelta,
    ToolCallFinished,
    ToolCallStarted,
    architect,
    functional_analyst,
    specs_generation,
)
from jri.core.exceptions import RepositoryStateError, SpecsError
from jri.core.notes import Notebook
from jri.core.specs import ACCEPTANCE_TRAILER
from tests.conftest import CreateRepository, RunGit
from tests.doubles.openai import (
    FakeClient,
    call,
    partial_reply,
    reply,
    response,
    stopped_stream,
    streamed_reply,
    thought,
)
from tests.doubles.settings import build_settings
from tests.doubles.workspace import install_workspace

type Progress = ReasoningDelta | Row
type Result = functional_analyst.Ambiguities | specs_generation.Unchanged | str | None
type Row = ToolCallStarted | ToolCallFinished

AMBIGUITIES = functional_analyst.Ambiguities(outcome="ambiguities", ambiguities=["JSON or plain text?"])
ARCHITECTURE_FILES = {"architecture/design.md": "# Design\n"}
# A draft placing a specification `Specs.write` refuses a model: Git
# reads a body holding a null character as binary, and the diff of a
# binary file names it and carries none of its content.
NULL_BODY_DRAFT = (
    "diff --git a/.jri/specs/functional/null.md b/.jri/specs/functional/null.md\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/.jri/specs/functional/null.md\n"
    "@@ -0,0 +1 @@\n"
    "+# Null\x00byte\n"
)
FUNCTIONAL_FILES = {"functional/behavior.md": "# Behavior\n"}
UPDATED_ARCHITECTURE_FILES = {"architecture/design.md": "# Design\nAdd a total accumulator.\n"}
UPDATED_FUNCTIONAL_FILES = {"functional/behavior.md": "# Behavior\nTotal output is supported.\n"}


def build_workspace(path: Path, create_repository: CreateRepository) -> None:
    create_repository(path)
    install_workspace(path)


def generate(
    client: FakeClient, stop_at: Row | None = None, cancelled: Event | None = None
) -> tuple[list[Progress], Result]:
    captured: list[Result] = []
    cancelled = cancelled or Event()
    settings = build_settings(client)

    def drive() -> Generator[Progress]:
        captured.append((yield from specs_generation.generate(settings, cancelled)))

    events: list[Progress] = []
    # A stop reaches the run while the row naming the step it
    # interrupts is the one on screen.
    for event in drive():
        events.append(event)
        if event == stop_at:
            cancelled.set()
    return events, captured[0]


def commit_specs() -> None:
    client = FakeClient([streamed_reply("Repository report")], parsed=[written_specs(), designed_architecture()])
    _, commit = generate(client)
    assert isinstance(commit, str)


def written_specs(
    files: Mapping[str, str] = FUNCTIONAL_FILES, deleted: Sequence[str] = ()
) -> functional_analyst.Output:
    return functional_analyst.Output(
        result=functional_analyst.Specifications(
            outcome="specifications",
            files=[functional_analyst.File(path=path, content=content) for path, content in files.items()],
            deleted_paths=list(deleted),
        )
    )


def designed_architecture(
    files: Mapping[str, str] = ARCHITECTURE_FILES, deleted: Sequence[str] = ()
) -> architect.Output:
    return architect.Output(result=drafted_architecture(files, deleted))


def drafted_architecture(
    files: Mapping[str, str] = ARCHITECTURE_FILES, deleted: Sequence[str] = ()
) -> architect.Architecture:
    return architect.Architecture(
        outcome="architecture",
        files=[architect.File(path=path, content=content) for path, content in files.items()],
        deleted_paths=list(deleted),
    )


def reported_issues(*issues: str) -> architect.Output:
    return architect.Output(result=architect.Issues(outcome="functional_specification_issues", issues=list(issues)))


def build_thinking_call(output: functional_analyst.Output | architect.Output, text: str) -> list[object]:
    return [thought(text), *response(reply(output.model_dump_json()))]


def read_prompts(client: FakeClient) -> list[str]:
    return [
        str(message.get("content", ""))
        for context in client.responses.inputs
        for message in cast("list[dict[str, object]]", context)
    ]


def read_rows(events: list[Progress]) -> list[tuple[str, str, str]]:
    return [(type(row).__name__, row.call_id, row.label) for row in events if not isinstance(row, ReasoningDelta)]


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
        ("ToolCallStarted", "functional-1", "Writing functional specifications from your project notes"),
        ("ToolCallFinished", "functional-1", "Found project details to clarify"),
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
        parsed=[written_specs(UPDATED_FUNCTIONAL_FILES), designed_architecture(UPDATED_ARCHITECTURE_FILES)],
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


# Handed notes it has already answered, a model returns the
# specifications the project holds -- the shape the prompt asks for
# when nothing needs changing. Git reads that as no change at all,
# which is the models' conclusion rather than a write that failed.
def test_reports_a_generation_that_changed_nothing(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    build_workspace(tmp_path, create_repository)
    commit_specs()
    accepted = run_git(tmp_path, "rev-parse", "HEAD")
    client = FakeClient([streamed_reply("Repository report")], parsed=[written_specs(), designed_architecture()])

    rows, result = generate(client)

    assert result == specs_generation.Unchanged()
    assert read_rows(rows)[-2:] == [
        ("ToolCallStarted", "commit", "Comparing the specifications with your project"),
        ("ToolCallFinished", "commit", "Your project already holds these specifications"),
    ]
    assert run_git(tmp_path, "rev-parse", "HEAD") == accepted
    assert not (tmp_path / paths.ACCEPTANCE_FILE).exists()
    assert not run_git(tmp_path, "status", "--short")


@pytest.mark.parametrize(
    "stop_at",
    [
        ToolCallStarted("functional-1", "Writing functional specifications from your project notes", "✍️"),
        ToolCallStarted("architecture-1", "Designing the project architecture", "📐"),
        ToolCallFinished("architecture-1", "Designed the project architecture", "done"),
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
    [lambda cancelled: [stopped_stream(cancelled)], lambda cancelled: [written_specs(), stopped_stream(cancelled)]],
    ids=["writing", "designing"],
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


# Every call to a model is a wait of its own, minutes long, so it says
# on screen who is working and on which round -- and it says it in a
# row of its own, opened when the call starts and closed on what that
# call answered.
def test_opens_and_closes_a_row_for_every_model_call(tmp_path: Path, create_repository: CreateRepository) -> None:
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
        ("ToolCallStarted", "functional-1", "Writing functional specifications from your project notes"),
        ("ToolCallFinished", "functional-1", "Wrote functional specifications from your project notes"),
        ("ToolCallStarted", "explorer", "Studying your existing project"),
        ("ToolCallFinished", "explorer", "Studied your existing project"),
        ("ToolCallStarted", "architecture-1", "Designing the project architecture"),
        ("ToolCallFinished", "architecture-1", "Found 3 issues in the functional specifications"),
        ("ToolCallStarted", "functional-2", "3 issues found. Rewriting the functional specifications (round 2)"),
        ("ToolCallFinished", "functional-2", "Rewrote the functional specifications (round 2)"),
        ("ToolCallStarted", "architecture-2", "Reviewing the project architecture against them (round 2)"),
        ("ToolCallFinished", "architecture-2", "Found 1 issues in the functional specifications"),
        ("ToolCallStarted", "functional-3", "1 issues found. Rewriting the functional specifications (round 3)"),
        ("ToolCallFinished", "functional-3", "Rewrote the functional specifications (round 3)"),
        ("ToolCallStarted", "architecture-3", "Reviewing the project architecture against them (round 3)"),
        ("ToolCallFinished", "architecture-3", "Found 2 issues in the functional specifications"),
        ("ToolCallStarted", "functional-4", "2 issues found. Rewriting the functional specifications (round 4)"),
        ("ToolCallFinished", "functional-4", "Rewrote the functional specifications (round 4)"),
        ("ToolCallStarted", "architecture-4", "Reviewing the project architecture against them (round 4)"),
        ("ToolCallFinished", "architecture-4", "Designed the project architecture"),
        ("ToolCallStarted", "commit", "Saving the specifications to your project"),
        ("ToolCallFinished", "commit", "Saved the specifications to your project"),
    ]
    # Saving is JRI's own work rather than a model's, so every other row
    # answers for exactly one call the run made.
    assert len([row for row in rows if isinstance(row, ToolCallStarted) and row.call_id != "commit"]) == len(
        client.responses.inputs
    )
    assert isinstance(result, str)


# A call has one row of its own and publishes whatever it says about
# its own reasoning while that row is open, so a thought is read under
# the row of the model that thought it. A run whose models publish
# nothing reads exactly as this one does with the thoughts taken out.
def test_carries_the_thinking_of_a_call_between_its_two_rows(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[
            build_thinking_call(written_specs(), "Weighing the totals."),
            build_thinking_call(designed_architecture(), "Weighing the layers."),
        ],
    )

    events, result = generate(client)

    assert isinstance(result, str)
    assert [
        ("Thought", event.text) if isinstance(event, ReasoningDelta) else (type(event).__name__, event.call_id)
        for event in events
    ] == [
        ("ToolCallStarted", "functional-1"),
        ("Thought", "Weighing the totals."),
        ("ToolCallFinished", "functional-1"),
        ("ToolCallStarted", "explorer"),
        ("ToolCallFinished", "explorer"),
        ("ToolCallStarted", "architecture-1"),
        ("Thought", "Weighing the layers."),
        ("ToolCallFinished", "architecture-1"),
        ("ToolCallStarted", "commit"),
        ("ToolCallFinished", "commit"),
    ]


def test_leaves_the_saving_row_open_when_the_project_blocks_the_commit(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient([streamed_reply("Repository report")], parsed=[written_specs(), designed_architecture()])
    rows: list[Progress] = []

    def block_the_project_once_the_design_lands() -> None:
        for row in specs_generation.generate(build_settings(client)):
            rows.append(row)
            if isinstance(row, ToolCallFinished) and row.call_id == "architecture-1":
                stray = tmp_path / paths.FUNCTIONAL_SPECS_DIR / "stray.md"
                stray.parent.mkdir(parents=True)
                stray.write_text("blocked")

    with pytest.raises(RepositoryStateError, match=r"stray\.md"):
        block_the_project_once_the_design_lands()

    assert read_rows(rows)[-2:] == [
        ("ToolCallFinished", "architecture-1", "Designed the project architecture"),
        ("ToolCallStarted", "commit", "Saving the specifications to your project"),
    ]


def test_finishes_the_open_round_when_ambiguities_appear(tmp_path: Path, create_repository: CreateRepository) -> None:
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
        ("ToolCallStarted", "functional-2", "2 issues found. Rewriting the functional specifications (round 2)"),
        ("ToolCallFinished", "functional-2", "Found project details to clarify"),
    ]
    assert [row.call_id for row in rows if isinstance(row, ToolCallStarted)] == [
        row.call_id for row in rows if isinstance(row, ToolCallFinished)
    ]


# The number a round opens with is the length of the list of issues
# that round is being sent, so the screen cannot claim a round is
# answering more or fewer than the analyst was handed.
def test_names_the_issues_the_round_it_opens_answers(tmp_path: Path, create_repository: CreateRepository) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[
            written_specs(),
            reported_issues("Undefined totals.", "Unclear export.", "Missing errors."),
            written_specs(),
            reported_issues("Missing errors."),
            written_specs(),
            designed_architecture(),
        ],
    )

    rows, result = generate(client)

    assert isinstance(result, str)
    assert [row.label for row in rows if isinstance(row, ToolCallStarted) and "functional" in row.call_id][1:] == [
        "3 issues found. Rewriting the functional specifications (round 2)",
        "1 issues found. Rewriting the functional specifications (round 3)",
    ]
    revisions = [prompt for prompt in read_prompts(client) if "Architect feedback:" in prompt]
    assert revisions[0].endswith("Architect feedback:\n  - Undefined totals.\n  - Unclear export.\n  - Missing errors.")
    assert revisions[1].endswith("Architect feedback:\n  - Missing errors.")


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

    revision = next(prompt for prompt in read_prompts(client) if "Architect feedback:" in prompt)
    assert "functional/behavior.md" in revision
    assert "# Behavior" in revision
    assert "Architect feedback:\n  - Undefined totals.\n  - Unclear export." in revision
    assert "Rejected functional draft:" not in revision


# A round writes onto the specifications standing in the run's
# worktree, so a file the analyst leaves out of its answer keeps
# whatever the round before it left there -- and on the first round
# that is the accepted baseline, which is what makes an answer naming
# one file a whole answer.
def test_keeps_the_accepted_specification_a_round_left_out(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[written_specs({**FUNCTIONAL_FILES, "functional/exports.md": "# Exports\n"}), designed_architecture()],
    )
    generate(client)
    Notebook(tmp_path / paths.NOTEBOOK_FILE).add(["Report the totals too."], "t1")

    _, result = generate(
        FakeClient(
            [streamed_reply("Repository report")],
            parsed=[written_specs(UPDATED_FUNCTIONAL_FILES), designed_architecture()],
        )
    )

    assert isinstance(result, str)
    assert (tmp_path / paths.FUNCTIONAL_SPECS_DIR / "exports.md").read_text() == "# Exports\n"
    assert run_git(tmp_path, "show", "--format=", "--name-only").splitlines() == [
        ".jri/notebook.yaml",
        ".jri/specs/functional/behavior.md",
    ]


# The round answering the architect edits the draft the architect
# rejected rather than writing the accepted baseline out again, so a
# file only the first round wrote survives into the commit and the file
# the second round changed carries the second round's words. Move the
# run's worktree back inside the loop and the first round's file is
# gone.
def test_polishes_the_draft_the_last_round_wrote(tmp_path: Path, create_repository: CreateRepository) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[
            written_specs({**FUNCTIONAL_FILES, "functional/exports.md": "# Exports\n"}),
            reported_issues("Undefined totals."),
            written_specs(UPDATED_FUNCTIONAL_FILES),
            designed_architecture(),
        ],
    )

    _, result = generate(client)

    assert isinstance(result, str)
    polish = next(prompt for prompt in read_prompts(client) if "Architect feedback:" in prompt)
    assert "functional/exports.md" in polish
    assert (tmp_path / paths.FUNCTIONAL_SPECS_DIR / "exports.md").read_text() == "# Exports\n"
    assert (tmp_path / paths.FUNCTIONAL_SPECS_DIR / "behavior.md").read_text() == (
        UPDATED_FUNCTIONAL_FILES["functional/behavior.md"]
    )


# The first round has no draft to write onto, so what it is shown is
# the specifications the project holds and nothing else.
def test_asks_the_first_round_for_specifications_against_the_accepted_baseline(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    commit_specs()
    Notebook(tmp_path / paths.NOTEBOOK_FILE).add(["Report the totals too."], "t1")
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[written_specs(UPDATED_FUNCTIONAL_FILES), designed_architecture(UPDATED_ARCHITECTURE_FILES)],
    )

    generate(client)

    first = read_prompts(client)[1]
    assert "Current functional specifications:" in first
    assert FUNCTIONAL_FILES["functional/behavior.md"] in first
    assert UPDATED_FUNCTIONAL_FILES["functional/behavior.md"] not in first


# What a run leaves behind is Git's own patch onto the specifications
# the project holds, so an ending that reaches no commit keeps the
# rounds that did finish -- and keeps them out of the project, which
# still holds only what it held before the run.
@pytest.mark.parametrize(
    "queue_tail",
    [[functional_analyst.Output(result=AMBIGUITIES)], [written_specs(), designed_architecture(FUNCTIONAL_FILES)]],
    ids=["ambiguities", "failure"],
)
def test_keeps_the_draft_a_run_that_reached_no_commit_wrote(
    queue_tail: list[object], tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient(
        [streamed_reply("Repository report")], parsed=[written_specs(), reported_issues("Unclear export."), *queue_tail]
    )

    with suppress(SpecsError):
        generate(client)

    assert "+# Behavior" in (tmp_path / paths.DRAFT_FILE).read_text()
    assert not (tmp_path / paths.SPECS_DIR).exists()


def test_keeps_the_draft_a_stopped_run_wrote(tmp_path: Path, create_repository: CreateRepository) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient([streamed_reply("Repository report")], parsed=[written_specs(), designed_architecture()])

    _, result = generate(client, ToolCallFinished("architecture-1", "Designed the project architecture", "done"))

    assert result is None
    draft = (tmp_path / paths.DRAFT_FILE).read_text()
    assert "+# Behavior" in draft
    assert "+# Design" in draft


def test_forgets_the_draft_a_run_committed(tmp_path: Path, create_repository: CreateRepository) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient([streamed_reply("Repository report")], parsed=[written_specs(), designed_architecture()])

    _, result = generate(client)

    assert isinstance(result, str)
    assert not (tmp_path / paths.DRAFT_FILE).exists()


# A run picking up a draft says so in a row of its own, and what it
# picks up is what the next round writes onto: the file the run before
# it drafted is in the analyst's context and in the commit, even though
# no round of this run ever wrote it.
def test_resumes_the_draft_a_run_left_behind(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    build_workspace(tmp_path, create_repository)
    stopped = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[written_specs({**FUNCTIONAL_FILES, "functional/exports.md": "# Exports\n"}), designed_architecture()],
    )
    generate(stopped, ToolCallFinished("architecture-1", "Designed the project architecture", "done"))
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[written_specs(UPDATED_FUNCTIONAL_FILES), designed_architecture(UPDATED_ARCHITECTURE_FILES)],
    )

    rows, result = generate(client)

    assert isinstance(result, str)
    assert read_rows(rows)[:2] == [
        ("ToolCallStarted", "resume", "Picking up the specifications a previous run drafted"),
        ("ToolCallFinished", "resume", "Picked up a draft of 3 specification files"),
    ]
    assert "functional/exports.md" in read_prompts(client)[1]
    assert run_git(tmp_path, "show", "--format=", "--name-only", "--", paths.SPECS_DIR).splitlines() == [
        ".jri/specs/architecture/design.md",
        ".jri/specs/functional/behavior.md",
        ".jri/specs/functional/exports.md",
    ]


# The row counts the draft, which is a delta: a specification the
# project already holds is one the checkout put there, and no part of
# what the run picked up. A mature specification tree would otherwise
# report forty files for a draft that changed one.
def test_counts_the_draft_rather_than_the_specifications_the_project_holds(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    commit_specs()
    stopped = FakeClient(
        [streamed_reply("Repository report")], parsed=[written_specs(UPDATED_FUNCTIONAL_FILES), designed_architecture()]
    )
    generate(stopped, ToolCallFinished("architecture-1", "Designed the project architecture", "done"))
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[written_specs(UPDATED_FUNCTIONAL_FILES), designed_architecture(UPDATED_ARCHITECTURE_FILES)],
    )

    rows, result = generate(client)

    assert isinstance(result, str)
    assert read_rows(rows)[1] == ("ToolCallFinished", "resume", "Picked up a draft of 1 specification file")


# A draft the project has moved past is refused whole, and refusing it
# is not a failure of the run that met it: the run writes from the
# specifications the project holds, commits, and never meets that draft
# again.
def test_starts_clean_when_the_drafted_specifications_no_longer_fit(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    build_workspace(tmp_path, create_repository)
    commit_specs()
    stopped = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[written_specs(UPDATED_FUNCTIONAL_FILES), designed_architecture(UPDATED_ARCHITECTURE_FILES)],
    )
    generate(stopped, ToolCallFinished("architecture-1", "Designed the project architecture", "done"))
    # Another accepted generation moves the specifications out from
    # under the draft, so what it says it is a delta onto is gone.
    (tmp_path / paths.FUNCTIONAL_SPECS_DIR / "behavior.md").write_text("# Totals\nThe project reports totals.\n")
    run_git(tmp_path, "add", "--force", paths.FUNCTIONAL_SPECS_DIR)
    run_git(tmp_path, "commit", "-qm", f"jri: update specifications\n\n{ACCEPTANCE_TRAILER}")
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[written_specs(UPDATED_FUNCTIONAL_FILES), designed_architecture(UPDATED_ARCHITECTURE_FILES)],
    )

    rows, result = generate(client)

    assert isinstance(result, str)
    assert read_rows(rows)[:2] == [
        ("ToolCallStarted", "resume", "Picking up the specifications a previous run drafted"),
        ("ToolCallFinished", "resume", "The drafted specifications no longer fit your project"),
    ]
    assert not (tmp_path / paths.DRAFT_FILE).exists()
    assert (tmp_path / paths.FUNCTIONAL_SPECS_DIR / "behavior.md").read_text() == (
        UPDATED_FUNCTIONAL_FILES["functional/behavior.md"]
    )
    assert not run_git(tmp_path, "status", "--short")


# A draft carrying what no answer of a model's could is refused like
# any other draft that does not fit. A run that met it only after
# picking it up would end over a specification nothing of JRI's wrote
# -- and since every ending but a commit keeps the draft, so would the
# run after it, and the one after that.
def test_starts_clean_when_the_draft_holds_what_jri_would_not_write(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    build_workspace(tmp_path, create_repository)
    commit_specs()
    (tmp_path / paths.DRAFT_FILE).write_text(NULL_BODY_DRAFT, encoding="utf-8", newline="\n")
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[written_specs(UPDATED_FUNCTIONAL_FILES), designed_architecture(UPDATED_ARCHITECTURE_FILES)],
    )

    rows, result = generate(client)

    assert isinstance(result, str)
    assert read_rows(rows)[:2] == [
        ("ToolCallStarted", "resume", "Picking up the specifications a previous run drafted"),
        ("ToolCallFinished", "resume", "The drafted specifications no longer fit your project"),
    ]
    assert not (tmp_path / paths.DRAFT_FILE).exists()
    assert (tmp_path / paths.FUNCTIONAL_SPECS_DIR / "behavior.md").read_text() == (
        UPDATED_FUNCTIONAL_FILES["functional/behavior.md"]
    )
    assert not run_git(tmp_path, "status", "--short")


# Every way a draft can be wrong ends the same way: the run says it
# could not be used and writes from the specifications the project
# holds. A draft JRI can neither read, nor remove, nor write over is
# the worst of them, and a run that stopped over it would meet the very
# same draft on every run after, with no way out but the user deleting
# a file JRI wrote.
def test_runs_through_a_draft_it_can_neither_read_nor_replace(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    build_workspace(tmp_path, create_repository)
    (tmp_path / paths.DRAFT_FILE).mkdir(parents=True)
    client = FakeClient([streamed_reply("Repository report")], parsed=[written_specs(), designed_architecture()])

    rows, result = generate(client)

    assert isinstance(result, str)
    assert read_rows(rows)[:2] == [
        ("ToolCallStarted", "resume", "Picking up the specifications a previous run drafted"),
        ("ToolCallFinished", "resume", "The drafted specifications no longer fit your project"),
    ]
    assert (tmp_path / paths.FUNCTIONAL_SPECS_DIR / "behavior.md").read_text() == "# Behavior\n"
    assert not run_git(tmp_path, "status", "--short")


def test_opens_no_row_for_a_run_with_no_draft_to_pick_up(tmp_path: Path, create_repository: CreateRepository) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient([streamed_reply("Repository report")], parsed=[written_specs(), designed_architecture()])

    rows, _ = generate(client)

    assert "resume" not in [call_id for _, call_id, _ in read_rows(rows)]


def test_asks_the_architect_to_finish_on_the_last_cycle(tmp_path: Path, create_repository: CreateRepository) -> None:
    build_workspace(tmp_path, create_repository)
    parsed: list[object] = [
        item
        for _ in range(specs_generation.MAX_CYCLES - 1)
        for item in (written_specs(), reported_issues("Unclear export."))
    ]
    parsed.extend([written_specs(), drafted_architecture()])
    client = FakeClient([streamed_reply("Repository report")], parsed=parsed)

    rows, result = generate(client)

    assert client.responses.options[-1]["text_format"] is architect.Architecture
    assert [row.call_id for row in rows if isinstance(row, ToolCallStarted) and "architecture" in row.call_id] == [
        f"architecture-{cycle}" for cycle in range(1, specs_generation.MAX_CYCLES + 1)
    ]
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


# The study's thinking is the model working; its report is the
# evidence the architect designs against. A thought reaches the reader
# as a thought and never the architect as a fact about the project.
def test_keeps_the_thinking_of_the_project_study_out_of_its_report(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient(
        [[thought("Weighing which files to read."), *streamed_reply("Repository report")]],
        parsed=[written_specs(), designed_architecture()],
    )

    events, result = generate(client)

    assert isinstance(result, str)
    assert ReasoningDelta("Weighing which files to read.") in events
    report = next(prompt for prompt in read_prompts(client) if "Repository analysis report:" in prompt)
    assert report.endswith("Repository analysis report:\n```\nRepository report\n```")
    assert "Weighing which files to read." not in report


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
    directory = Path(instructions.split("Working directory:\n```\n")[1].split("\n```\n")[0])
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


def test_refuses_an_analyst_that_deletes_every_functional_specification(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    commit_specs()
    client = FakeClient([], parsed=[written_specs({}, list(FUNCTIONAL_FILES))])

    with pytest.raises(SpecsError, match="Functional specifications cannot be empty"):
        generate(client)


def test_refuses_an_architect_that_deletes_every_architecture_specification(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    commit_specs()
    client = FakeClient(
        [streamed_reply("Repository report")],
        parsed=[written_specs(UPDATED_FUNCTIONAL_FILES), designed_architecture({}, list(ARCHITECTURE_FILES))],
    )

    with pytest.raises(SpecsError, match="Architecture specifications cannot be empty"):
        generate(client)


def test_refuses_functional_specifications_that_leave_their_root(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient([], parsed=[written_specs(ARCHITECTURE_FILES)])

    with pytest.raises(SpecsError, match=r"cannot change `architecture/design\.md`"):
        generate(client)


def test_refuses_an_architecture_that_leaves_its_root(tmp_path: Path, create_repository: CreateRepository) -> None:
    build_workspace(tmp_path, create_repository)
    client = FakeClient(
        [streamed_reply("Repository report")], parsed=[written_specs(), designed_architecture(FUNCTIONAL_FILES)]
    )

    with pytest.raises(SpecsError, match=r"cannot change `functional/behavior\.md`"):
        generate(client)
