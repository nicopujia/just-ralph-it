import logging
from collections.abc import Generator
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path, PurePosixPath
from threading import Event

from jri.core import ai, paths
from jri.core.ai import architect, functional_analyst
from jri.core.exceptions import PersistenceError, SpecsError
from jri.core.notes import Notebook
from jri.core.settings import Settings
from jri.core.specs import Baseline, Specs
from jri.lib import git

# This stream reports opened and closed rows and model reasoning. It excludes `TextDelta`.
# Only the interviewer sends replies to the user.
type Progress = ai.ReasoningDelta | ai.ToolCallStarted | ai.ToolCallFinished
type SpecsResult = Ambiguities | Unchanged | str

MAX_CYCLES = 10

logger = logging.getLogger(__name__)


# Check cancellation during model calls and between steps. Stopping before `Specs.accept` leaves the project unchanged.
# Do not stop acceptance. A partial patch or index is worse.
def generate(settings: Settings, cancelled: Event | None = None) -> Generator[Progress, None, SpecsResult | None]:
    cancelled = cancelled or Event()
    specs = Specs(Path.cwd())
    baseline = specs.prepare()
    explorer_report: str | None = None

    cycle = 0

    # Use one worktree for the run. Each round edits its draft.
    # Save each change so a later run can continue. The draft is the agreement between rounds.
    with specs.repository.open_worktree(baseline.commit, location=specs.workspace.reserve_worktree_dir()) as staging:
        yield from _resume(specs, staging)
        functional_context = _build_functional_context(specs, baseline, staging)

        # The final cycle asks the architect to finish. It always returns architecture, so the loop returns a result.
        while True:
            cycle += 1
            logger.info("specs_cycle_started cycle=%d", cycle)
            # Each row identifies one model call by cycle. Its feedback count equals the list sent to the analyst.
            # This keeps the UI count accurate.
            yield ai.ToolCallStarted(
                f"functional-{cycle}", _describe_writing(cycle, len(functional_context.architect_feedback or ())), "✍️"
            )
            # A fresh agent per cycle keeps each call stateless, matching the fresh input built for it.
            analyst = functional_analyst.FunctionalAnalyst(
                settings,
                staging,
                changed=functional_context.notebook_diff is not None,
                existing=functional_context.current_specs_index is not None,
                feedback=bool(functional_context.architect_feedback),
            )
            functional_result = yield from analyst.write(functional_context, cancelled)
            if functional_result is None:
                return None
            # A pass with questions and no file has nothing to save. Ask them, and leave the project as it stands.
            # A pass with neither is a pass that did nothing. `Specs.write` refuses it below.
            if functional_result.unresolved and not (functional_result.files or functional_result.deleted_paths):
                logger.info("specs_ambiguities cycle=%d count=%d", cycle, len(functional_result.unresolved))
                yield ai.ToolCallFinished(f"functional-{cycle}", "Found project details to clarify", "done")
                return Ambiguities(list(functional_result.unresolved))
            yield ai.ToolCallFinished(f"functional-{cycle}", _describe_written(cycle), "done")

            specs.write(
                staging,
                {file.path: Specs.format(file) for file in functional_result.files},
                functional_result.deleted_paths,
                paths.FUNCTIONAL_SPECS_ROOT,
            )
            functional = specs.read(staging, paths.FUNCTIONAL_SPECS_DIR)
            if not functional:
                raise SpecsError("Functional specifications cannot be empty.")
            specs.save_draft(staging, baseline)

            # The pass wrote what the notebook settles and left the rest to the user.
            # Stop after the draft, so the run the answers start continues from this work.
            # Ask before the study and the design, which cannot settle a question that only the user answers.
            if functional_result.unresolved:
                logger.info("specs_unresolved cycle=%d count=%d", cycle, len(functional_result.unresolved))
                yield ai.ToolCallStarted("clarify", "Checking the project details to clarify", "❓")
                yield ai.ToolCallFinished(
                    "clarify", _describe_clarifications(len(functional_result.unresolved)), "done"
                )
                return Ambiguities(list(functional_result.unresolved))

            if explorer_report is None:
                yield ai.ToolCallStarted("explorer", "Studying your existing project", "🔎")
                # This row is nested under the explorer row. Closing it closes nested rows.
                # Study a disposable copy of the current project, not JRI's current commit.
                snapshot = specs.workspace.root / paths.SNAPSHOT_DIR
                with specs.repository.open_worktree(None, location=snapshot) as project:
                    explorer_report = (
                        yield from ai.Explorer(settings, project.path).report(
                            "Study this repository generally. Report its structure, architecture, established "
                            "patterns, development commands, and the constraints that new work in it must respect.",
                            depth=1,
                            cancelled=cancelled,
                        )
                    ).strip()
                # A cancelled study can be incomplete. Do not report the run as broken for that reason.
                if cancelled.is_set():
                    return None
                if not explorer_report:
                    raise SpecsError("Repository exploration produced no report.")
                yield ai.ToolCallFinished("explorer", "Studied your existing project", "done")

            yield ai.ToolCallStarted(f"architecture-{cycle}", _describe_designing(cycle), "📐")
            designer = architect.Architect(settings, staging, final=cycle == MAX_CYCLES)
            architecture_result = yield from designer.design(
                architect.Input(
                    functional_specs_index=specs.index(functional),
                    current_architecture_index=specs.index(specs.read(staging, paths.ARCHITECTURE_SPECS_DIR)),
                    tracked_repository_tree=list(specs.repository.read_worktree_paths()),
                    explorer_report=explorer_report,
                ),
                cancelled,
            )
            if architecture_result is None:
                return None
            if isinstance(architecture_result, architect.Issues):
                logger.info("specs_issues cycle=%d count=%d", cycle, len(architecture_result.issues))
                # A pass with issues has no architecture. Close the row with its actual result, not a missing design.
                yield ai.ToolCallFinished(
                    f"architecture-{cycle}",
                    f"Found {len(architecture_result.issues)} issues in the functional specifications",
                    "done",
                )
                functional_context = functional_context.model_copy(
                    update={
                        "current_specs_index": specs.index(functional),
                        "architect_feedback": architecture_result.issues,
                    }
                )
                continue

            specs.write(
                staging,
                {file.path: Specs.format(file) for file in architecture_result.files},
                architecture_result.deleted_paths,
                paths.ARCHITECTURE_SPECS_ROOT,
            )
            if not specs.read(staging, paths.ARCHITECTURE_SPECS_DIR):
                raise SpecsError("Architecture specifications cannot be empty.")
            patch = specs.save_draft(staging, baseline)

            yield ai.ToolCallFinished(f"architecture-{cycle}", "Designed the project architecture", "done")
            # This is the last safe cancellation point. After the next row, the specifications apply to the project.
            # The run must then finish.
            if cancelled.is_set():
                return None
            # An empty patch means the models found that the project already has these specifications.
            # `git apply` rejects empty patches, and no acceptance record can undo one.
            if not patch:
                logger.info("specs_unchanged cycles=%d", cycle)
                yield ai.ToolCallStarted("commit", "Comparing the specifications with your project", "💾")
                yield ai.ToolCallFinished("commit", "Your project already holds these specifications", "done")
                return Unchanged()
            # Saving is a separate step. If the project blocks the commit, close the save row, not the design row.
            yield ai.ToolCallStarted("commit", "Saving the specifications to your project", "💾")
            commit = specs.accept(patch, baseline)
            yield ai.ToolCallFinished("commit", "Saved the specifications to your project", "done")
            return commit


# These are the behavioral decisions that only the user can take. The run stops, and the interviewer asks them.
# A run can carry these and still leave its specifications in the draft, so this marks no lost work.
@dataclass(frozen=True)
class Ambiguities:
    ambiguities: list[str]


# This marks a generation that wrote specifications already in the project.
# It made no commit because no commit was needed.
@dataclass(frozen=True)
class Unchanged: ...


# Show a resume in its own row. Count files after Git writes them, not from its exit status.
# An obsolete draft closes the row empty and starts from project specifications.
def _resume(specs: Specs, staging: git.Repository) -> Generator["ai.ToolCallStarted | ai.ToolCallFinished"]:
    if not specs.drafted:
        return
    yield ai.ToolCallStarted("resume", "Picking up the specifications a previous run drafted", "↩️")
    drafted = specs.resume(staging)
    if drafted is None:
        yield ai.ToolCallFinished("resume", "The drafted specifications no longer fit your project", "empty")
        return
    logger.info("draft_resumed files=%d", len(drafted))
    yield ai.ToolCallFinished("resume", _describe_picked_up(len(drafted)), "done")


def _build_functional_context(specs: Specs, baseline: Baseline, staging: git.Repository) -> functional_analyst.Input:
    existing = specs.read(staging, paths.FUNCTIONAL_SPECS_DIR)
    notebook = Notebook.exclude_trashed(baseline.notebook)
    return functional_analyst.Input(
        notebook=notebook,
        notebook_diff=_diff_notebook(baseline, notebook),
        # Give a first pass no specification index at all. It writes the specifications that the project has none of.
        current_specs_index=specs.index(existing) if existing else None,
    )


# A run with no accepted baseline has no earlier notebook to compare with. A diff against nothing would only repeat
# the notebook the pass already reads, and it would name a baseline the project does not hold.
# Do not give trashed topics to the analyst. Filter both notebooks before the diff.
# Otherwise, old trashed topics appear as changes.
def _diff_notebook(baseline: Baseline, notebook: str) -> str | None:
    if baseline.accepted is None:
        return None
    try:
        accepted_notebook = Notebook.exclude_trashed(baseline.accepted_notebook)
    # An unreadable accepted notebook cannot show changes. Use the same state as a first generation.
    except PersistenceError:
        return None
    return "".join(
        unified_diff(
            accepted_notebook.splitlines(keepends=True),
            notebook.splitlines(keepends=True),
            fromfile=f"a/{PurePosixPath(paths.NOTEBOOK_FILE).name}",
            tofile=f"b/{PurePosixPath(paths.NOTEBOOK_FILE).name}",
        )
    )


def _describe_clarifications(details: int) -> str:
    if details == 1:
        return "Found 1 project detail to clarify"
    return f"Found {details} project details to clarify"


def _describe_picked_up(files: int) -> str:
    if files == 1:
        return "Picked up a draft of 1 specification file"
    return f"Picked up a draft of {files} specification files"


# Only the first round uses the notes alone. Later rounds answer the prior round.
# Include the round number in the transcript.
def _describe_writing(cycle: int, issues: int) -> str:
    if cycle == 1:
        return "Writing functional specifications from your project notes"
    return f"{issues} issues found. Rewriting the functional specifications (round {cycle})"


def _describe_written(cycle: int) -> str:
    if cycle == 1:
        return "Wrote functional specifications from your project notes"
    return f"Rewrote the functional specifications (round {cycle})"


def _describe_designing(cycle: int) -> str:
    if cycle == 1:
        return "Designing the project architecture"
    return f"Reviewing the project architecture against them (round {cycle})"
