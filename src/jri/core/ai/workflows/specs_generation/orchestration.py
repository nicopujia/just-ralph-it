import logging
from collections.abc import Generator
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path, PurePosixPath
from threading import Event

from jri.core import ai, paths
from jri.core.exceptions import PersistenceError, SpecsError
from jri.core.notes import Notebook
from jri.core.settings import Settings
from jri.core.specs import Baseline, Specs
from jri.lib import git

from . import architect, functional_analyst

type SpecsResult = functional_analyst.Ambiguities | Unchanged | str

MAX_CYCLES = 10

logger = logging.getLogger(__name__)


# A stop is answered inside the model call as well as between the
# steps, so the longest it waits is the stream event already in
# flight rather than the minutes a whole call takes. A step that
# answers with nothing is a step the user stopped, and a run that
# stops leaves the project as it found it: everything a run writes
# before `Specs.accept` is written in a worktree the run throws away,
# and the acceptance itself is the one step no stop interrupts, since
# a half-applied patch or a staged index would be worse than never
# stopping at all.
def generate(
    settings: Settings, cancelled: Event | None = None
) -> Generator["ai.ToolCallStarted | ai.ToolCallFinished", None, SpecsResult | None]:
    cancelled = cancelled or Event()
    specs = Specs(Path.cwd())
    analyst = functional_analyst.FunctionalAnalyst(settings)
    designer = architect.Architect(settings)
    baseline = specs.prepare()
    explorer_report: str | None = None

    cycle = 0

    # One worktree for the whole run, and the specifications it holds
    # are what every round writes onto. So a round answering the
    # architect edits the draft the architect read, and what a run that
    # ends early leaves behind is the draft as far as it got, saved
    # after every write for the next run to pick up. Nothing is carried
    # between the rounds as a list of what they settled: the draft is
    # the settlement.
    with specs.repository.open_worktree(baseline.commit) as staging:
        yield from _resume(specs, staging)
        functional_context = _build_functional_context(specs, baseline, staging)

        # The last cycle asks the architect to finish, which always
        # answers with an architecture, so the loop always ends with a
        # result.
        while True:
            cycle += 1
            logger.info("specs_cycle_started cycle=%d", cycle)
            # A row is one model call, named after the cycle rather than
            # held in a variable the call after it overwrites: a row two
            # calls share names the wrong agent for the whole of the
            # second. What it says it is answering is the length of the
            # very list the analyst is being sent, so the number on
            # screen cannot disagree with what it was asked to fix.
            yield ai.ToolCallStarted(
                f"functional-{cycle}", _describe_writing(cycle, len(functional_context.architect_feedback or ())), "✍️"
            )
            functional_result = analyst.write(functional_context, cancelled)
            if functional_result is None:
                return None
            if isinstance(functional_result, functional_analyst.Ambiguities):
                logger.info("specs_ambiguities cycle=%d count=%d", cycle, len(functional_result.ambiguities))
                yield ai.ToolCallFinished(f"functional-{cycle}", "Found project details to clarify", "done")
                return functional_result
            yield ai.ToolCallFinished(f"functional-{cycle}", _describe_written(cycle), "done")

            specs.write(
                staging,
                {file.path: file.content for file in functional_result.files},
                functional_result.deleted_paths,
                paths.FUNCTIONAL_SPECS_ROOT,
            )
            functional = specs.read(staging.path, paths.FUNCTIONAL_SPECS_DIR)
            if not functional:
                raise SpecsError("Functional specifications cannot be empty.")
            specs.save_draft(staging, baseline)

            if explorer_report is None:
                yield ai.ToolCallStarted("explorer", "Studying your existing project", "🔎")
                # Nested under the row above, so closing that row
                # clears the rows the run left behind. The study runs
                # in a throwaway copy of the project as it stands on
                # disk: whatever a command of its own writes there dies
                # with the copy instead of dirtying the project, and
                # what the architect designs against is the project the
                # user has rather than the commit JRI happens to sit on.
                with specs.repository.open_worktree(None) as project:
                    explorer_report = (
                        yield from ai.Explorer(settings, project.path).report(
                            "Study this repository generally. Report its structure, architecture, established "
                            "patterns, development commands, and the constraints that new work in it must respect.",
                            depth=1,
                            cancelled=cancelled,
                        )
                    ).strip()
                # A study the user stopped is short of what it would
                # have reported, which is no reason to call the run
                # broken.
                if cancelled.is_set():
                    return None
                if not explorer_report:
                    raise SpecsError("Repository exploration produced no report.")
                yield ai.ToolCallFinished("explorer", "Studied your existing project", "done")

            yield ai.ToolCallStarted(f"architecture-{cycle}", _describe_designing(cycle), "📐")
            architecture_result = (designer.finish if cycle == MAX_CYCLES else designer.design)(
                architect.Input(
                    functional_specs=specs.render(functional),
                    current_architecture=specs.render(specs.read(staging.path, paths.ARCHITECTURE_SPECS_DIR)),
                    tracked_repository_tree=list(specs.repository.read_worktree_paths()),
                    explorer_report=explorer_report,
                ),
                cancelled,
            )
            if architecture_result is None:
                return None
            if isinstance(architecture_result, architect.Issues):
                logger.info("specs_issues cycle=%d count=%d", cycle, len(architecture_result.issues))
                # A pass that found issues wrote no architecture, so the
                # row closes on what the call answered rather than on a
                # design nothing holds.
                yield ai.ToolCallFinished(
                    f"architecture-{cycle}",
                    f"Found {len(architecture_result.issues)} issues in the functional specifications",
                    "done",
                )
                functional_context = functional_context.model_copy(
                    update={"current_specs": specs.render(functional), "architect_feedback": architecture_result.issues}
                )
                continue

            specs.write(
                staging,
                {file.path: file.content for file in architecture_result.files},
                architecture_result.deleted_paths,
                paths.ARCHITECTURE_SPECS_ROOT,
            )
            if not specs.read(staging.path, paths.ARCHITECTURE_SPECS_DIR):
                raise SpecsError("Architecture specifications cannot be empty.")
            patch = specs.save_draft(staging, baseline)

            yield ai.ToolCallFinished(f"architecture-{cycle}", "Designed the project architecture", "done")
            # The last moment a stop still costs the user nothing but
            # the run: past the row below, the specifications are on
            # their way into the project and the run sees it through.
            if cancelled.is_set():
                return None
            # A generation that changes nothing is what the models
            # concluded, not a failure of anyone's: they read the notes
            # and wrote the specifications the project already holds.
            # Git says so with an empty diff, and `git apply` refuses
            # one -- so an acceptance over it would end the turn blaming
            # a write that never happened, over a record no undo of that
            # same empty patch can take back.
            if not patch:
                logger.info("specs_unchanged cycles=%d", cycle)
                yield ai.ToolCallStarted("commit", "Comparing the specifications with your project", "💾")
                yield ai.ToolCallFinished("commit", "Your project already holds these specifications", "done")
                return Unchanged()
            # Saving is a step of its own, so a project state that
            # blocks the commit closes the row naming it rather than the
            # design row, whose work was already done and is nowhere at
            # fault.
            yield ai.ToolCallStarted("commit", "Saving the specifications to your project", "💾")
            commit = specs.accept(patch, baseline)
            yield ai.ToolCallFinished("commit", "Saved the specifications to your project", "done")
            return commit


# What a generation concluded when the specifications it wrote are
# the ones the project already holds: no commit was made, because
# there was nothing to commit.
@dataclass(frozen=True)
class Unchanged: ...


# A run picking up where another left off says so where it says
# everything else, in a row of its own, and the number it closes with
# is the specifications the tree holds that the checkout did not --
# read back after Git wrote, never inferred from how Git ended. A draft
# the project has moved past is no failure of this run's: it closes the
# row empty and the run writes from the specifications the project
# holds, which is exactly where a run with no draft starts. A run with
# nothing to pick up opens no row at all, so the row appearing is
# itself the news that this run resumed.
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


# A trashed topic is thinking the user threw away, so the analyst
# reads the notebook without it -- on both sides of the diff, since a
# document filtered against a raw one reports every topic ever trashed
# as a change this generation has to answer for. The specifications it
# writes onto are the ones standing in the run's own worktree, which is
# the accepted baseline until a draft or an earlier round moves it.
def _build_functional_context(specs: Specs, baseline: Baseline, staging: git.Repository) -> functional_analyst.Input:
    notebook = Notebook.exclude_trashed(baseline.notebook)
    try:
        accepted_notebook = Notebook.exclude_trashed(baseline.accepted_notebook)
    # A notebook JRI can no longer read says nothing about what
    # changed, and there is already a state for that: the diff a first
    # generation shows, against no accepted baseline at all.
    except PersistenceError:
        accepted_notebook = ""
    return functional_analyst.Input(
        notebook=notebook,
        notebook_diff="".join(
            unified_diff(
                accepted_notebook.splitlines(keepends=True),
                notebook.splitlines(keepends=True),
                fromfile=f"a/{PurePosixPath(paths.NOTEBOOK_FILE).name}",
                tofile=f"b/{PurePosixPath(paths.NOTEBOOK_FILE).name}",
            )
        ),
        current_specs=specs.render(specs.read(staging.path, paths.FUNCTIONAL_SPECS_DIR)),
    )


def _describe_picked_up(files: int) -> str:
    if files == 1:
        return "Picked up a draft of 1 specification file"
    return f"Picked up a draft of {files} specification files"


# The first round is the only one the notes alone explain; every round
# after it answers the round before, and carries its number so that a
# transcript of eight rows says which of them is which.
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
