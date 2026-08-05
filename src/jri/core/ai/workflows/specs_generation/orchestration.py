import logging
from collections.abc import Callable, Generator
from difflib import unified_diff
from functools import partial
from pathlib import Path, PurePosixPath

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


def generate(settings: Settings) -> Generator["ai.ToolCallStarted | ai.ToolCallFinished", None, SpecsResult]:
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
        functional_result = analyst.write(functional_context)
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
            )
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
                        )
                    ).strip()
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
            architecture_result = designer.finish(context) if cycle == MAX_CYCLES else designer.design(context)
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
            )
            if not specs.read(staging.path, paths.ARCHITECTURE_SPECS_DIR):
                raise SpecsError("Architecture specifications cannot be empty.")
            patch = staging.diff(baseline.commit, paths=(paths.FUNCTIONAL_SPECS_DIR, paths.ARCHITECTURE_SPECS_DIR))

        yield ai.ToolCallFinished(
            open_row.call_id, "Designed the project architecture" if cycle == 1 else open_row.label, "done"
        )
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
    specs: Specs, staging: git.Repository, root: str, patch: str, repair: Callable[[str, str], str]
) -> None:
    for attempt in range(1, MAX_PATCH_ATTEMPTS + 1):
        try:
            specs.apply(staging, patch, root)
        except git.Error as error:
            if attempt == MAX_PATCH_ATTEMPTS:
                raise SpecsError(
                    f"Git rejected the {root} specification patch on all {MAX_PATCH_ATTEMPTS} attempts:\n{error}"
                ) from error
            logger.info("patch_repair_requested root=%s attempt=%d", root, attempt)
            patch = repair(patch, str(error))
        else:
            return
