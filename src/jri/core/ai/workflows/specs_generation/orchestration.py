import logging
from collections.abc import Callable, Generator
from difflib import unified_diff
from functools import partial
from pathlib import Path, PurePosixPath
from threading import Event

from jri.core import ai, paths
from jri.core.exceptions import SpecsError
from jri.core.settings import Settings
from jri.core.specs import Specs
from jri.lib import git

from . import architect, functional_analyst

type SpecsResult = functional_analyst.Ambiguities | str

MAX_CYCLES = 10
MAX_PATCH_ATTEMPTS = 3

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
    functional_context = functional_analyst.Input(
        notebook=baseline.notebook.decode(),
        notebook_diff="".join(
            unified_diff(
                baseline.accepted_notebook.decode().splitlines(keepends=True),
                baseline.notebook.decode().splitlines(keepends=True),
                fromfile=f"a/{PurePosixPath(paths.NOTEBOOK_FILE).name}",
                tofile=f"b/{PurePosixPath(paths.NOTEBOOK_FILE).name}",
            )
        ),
        accepted_specs=specs.render(baseline.functional),
    )
    open_row = ai.ToolCallStarted("functional", "Writing functional specifications from your project notes", "✍️")

    cycle = 0

    # The last cycle asks the architect to finish, which always
    # answers with a patch, so the loop always ends with a result.
    while True:
        cycle += 1
        logger.info("specs_cycle_started cycle=%d", cycle)
        if cycle == 1:
            yield open_row
        functional_result = analyst.write(functional_context, cancelled)
        if functional_result is None:
            return None
        if isinstance(functional_result, functional_analyst.Ambiguities):
            logger.info("specs_ambiguities cycle=%d count=%d", cycle, len(functional_result.ambiguities))
            yield ai.ToolCallFinished(open_row.call_id, "Found project details to clarify", "done")
            return functional_result
        if cycle == 1:
            yield ai.ToolCallFinished(
                open_row.call_id, "Wrote functional specifications from your project notes", "done"
            )

        with specs.repository.open_worktree(baseline.commit) as staging:
            _apply_patch(
                specs,
                staging,
                paths.FUNCTIONAL_SPECS_ROOT,
                functional_result.patch,
                partial(analyst.repair, functional_context),
                cancelled,
            )
            if cancelled.is_set():
                return None
            functional = specs.read(staging.path, paths.FUNCTIONAL_SPECS_DIR)
            if not functional:
                raise SpecsError("Functional specifications cannot be empty.")

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
                open_row = ai.ToolCallStarted("architecture", "Designing the project architecture", "📐")
                yield open_row

            context = architect.Input(
                functional_specs=specs.render(functional),
                accepted_architecture=specs.render(baseline.architecture),
                tracked_repository_tree=list(specs.repository.read_worktree_paths()),
                explorer_report=explorer_report,
            )
            architecture_result = (
                designer.finish(context, cancelled) if cycle == MAX_CYCLES else designer.design(context, cancelled)
            )
            if architecture_result is None:
                return None
            if isinstance(architecture_result, architect.Issues):
                logger.info("specs_issues cycle=%d count=%d", cycle, len(architecture_result.issues))
                # A polish row has no separate closing phrasing,
                # so it closes under the label it opened with.
                yield ai.ToolCallFinished(
                    open_row.call_id, "Drafted the project architecture" if cycle == 1 else open_row.label, "done"
                )
                open_row = ai.ToolCallStarted(
                    f"polish-{cycle}",
                    f"{len(architecture_result.issues)} issues found. Polishing... (round {cycle})",
                    "🗒️",
                )
                yield open_row
                functional_context = functional_context.model_copy(
                    update={
                        "rejected_specs": specs.render(functional),
                        "architect_feedback": architecture_result.issues,
                    }
                )
                continue

            _apply_patch(
                specs,
                staging,
                paths.ARCHITECTURE_SPECS_ROOT,
                architecture_result.patch,
                partial(designer.repair, context),
                cancelled,
            )
            if cancelled.is_set():
                return None
            if not specs.read(staging.path, paths.ARCHITECTURE_SPECS_DIR):
                raise SpecsError("Architecture specifications cannot be empty.")
            patch = staging.diff(baseline.commit, paths=(paths.FUNCTIONAL_SPECS_DIR, paths.ARCHITECTURE_SPECS_DIR))

        yield ai.ToolCallFinished(
            open_row.call_id, "Designed the project architecture" if cycle == 1 else open_row.label, "done"
        )
        # The last moment a stop still costs the user nothing but the
        # run: past the row below, the specifications are on their way
        # into the project and the run sees it through.
        if cancelled.is_set():
            return None
        # Saving is a step of its own, so a project state that blocks
        # the commit closes the row naming it rather than the design
        # row, whose work was already done and is nowhere at fault.
        yield ai.ToolCallStarted("commit", "Saving the specifications to your project", "💾")
        commit = specs.accept(patch, baseline)
        yield ai.ToolCallFinished("commit", "Saved the specifications to your project", "done")
        return commit


# A diff a model got slightly wrong is its mistake to correct, not a
# reason to throw the whole run away, so the rejection goes back to
# the model that wrote it. The safety validation runs on every try,
# since a repaired patch is no more trusted than the first one.
def _apply_patch(
    specs: Specs,
    staging: git.Repository,
    root: str,
    patch: str,
    repair: Callable[[str, str, Event], str | None],
    cancelled: Event,
) -> None:
    for attempt in range(1, MAX_PATCH_ATTEMPTS + 1):
        try:
            specs.apply(staging, patch, root)
        except git.Error as error:
            if attempt == MAX_PATCH_ATTEMPTS:
                # Git's own rejection is a fact about a diff the user
                # never saw, and it is already in the log twice over.
                # What is theirs to know is that a run of theirs ended
                # and what it left behind, which is nothing.
                raise SpecsError(
                    f"JRI could not write the {root} specifications it drafted, after "
                    f"{MAX_PATCH_ATTEMPTS} attempts. Nothing was committed. Your notes stand, and your "
                    "project keeps the specifications it already had."
                ) from error
            logger.info("patch_repair_requested root=%s attempt=%d", root, attempt)
            repaired = repair(patch, str(error), cancelled)
            # A repair the user stopped leaves the patch unapplied in a
            # worktree the run is about to throw away, and the caller
            # reads the stop rather than being told about it twice.
            if repaired is None:
                return
            patch = repaired
        else:
            return
