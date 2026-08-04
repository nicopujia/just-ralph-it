import logging
from collections.abc import Generator
from difflib import unified_diff
from pathlib import PurePosixPath

from jri.core import ai, paths
from jri.core.exceptions import SpecsError
from jri.core.settings import Settings
from jri.core.specs import Specs

from . import architect, functional_analyst

type SpecsResult = functional_analyst.Ambiguities | str

MAX_CYCLES = 10

logger = logging.getLogger(__name__)


def generate(
    settings: Settings, active_commit: str | None
) -> Generator["ai.ToolCallStarted | ai.ToolCallFinished", None, SpecsResult]:
    specs = Specs(settings.cwd)
    analyst = functional_analyst.FunctionalAnalyst(settings)
    designer = architect.Architect(settings)
    baseline = specs.prepare(active_commit)
    explorer_report: str | None = None
    feedback: str | None = None
    rejected: str | None = None
    open_row = ai.ToolCallStarted("functional", "Writing functional specifications from your project notes", "✍️")

    cycle = 0

    # The last cycle asks the architect to finish, which always
    # answers with a patch, so the loop always ends with a result.
    while True:
        cycle += 1
        logger.info("specs_cycle_started cycle=%d", cycle)
        if cycle == 1:
            yield open_row
        functional_result = analyst.write(
            functional_analyst.Input(
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
                rejected_specs=rejected,
                architect_feedback=feedback,
            )
        )
        if isinstance(functional_result, functional_analyst.Ambiguities):
            logger.info("specs_ambiguities cycle=%d count=%d", cycle, len(functional_result.ambiguities))
            yield ai.ToolCallFinished(open_row.call_id, "Found project details to clarify")
            return functional_result
        if cycle == 1:
            yield ai.ToolCallFinished(open_row.call_id, "Wrote functional specifications from your project notes")

        with specs.repository.open_worktree(baseline.commit) as staging:
            specs.apply(staging, functional_result.patch, paths.FUNCTIONAL_SPECS_ROOT)
            functional = specs.read(staging.path, paths.FUNCTIONAL_SPECS_DIR)
            if not functional:
                raise SpecsError("Functional specifications cannot be empty.")

            if explorer_report is None:
                yield ai.ToolCallStarted("explorer", "Studying your existing project", "🔎")
                # Nested under the row above, so closing that row
                # clears the rows the run left behind.
                explorer_report = (
                    yield from ai.Explorer(settings.model_copy(update={"cwd": staging.path})).report(
                        "Study this repository generally. Report its structure, architecture, established "
                        "patterns, development commands, and the constraints that new work in it must respect.",
                        depth=1,
                    )
                ).strip()
                if not explorer_report:
                    raise SpecsError("Repository exploration produced no report.")
                yield ai.ToolCallFinished("explorer", "Studied your existing project")
                open_row = ai.ToolCallStarted("architecture", "Designing the project architecture", "📐")
                yield open_row

            context = architect.Input(
                functional_specs=specs.render(functional),
                accepted_architecture=specs.render(baseline.architecture),
                tracked_tree="\n".join(
                    specs.repository.read_tracked_paths(baseline.commit)
                    if baseline.commit
                    else specs.repository.read_worktree_paths()
                ),
                explorer_report=explorer_report,
            )
            architecture_result = designer.finish(context) if cycle == MAX_CYCLES else designer.design(context)
            if isinstance(architecture_result, architect.Issues):
                logger.info("specs_issues cycle=%d count=%d", cycle, len(architecture_result.issues))
                # A polish row has no separate closing phrasing,
                # so it closes under the label it opened with.
                yield ai.ToolCallFinished(
                    open_row.call_id, "Drafted the project architecture" if cycle == 1 else open_row.label
                )
                open_row = ai.ToolCallStarted(
                    f"polish-{cycle}",
                    f"{len(architecture_result.issues)} issues found. Polishing... (round {cycle})",
                    "🗒️",
                )
                yield open_row
                rejected = specs.render(functional)
                feedback = "\n".join(f"- {issue}" for issue in architecture_result.issues)
                continue

            specs.apply(staging, architecture_result.patch, paths.ARCHITECTURE_SPECS_ROOT)
            if not specs.read(staging.path, paths.ARCHITECTURE_SPECS_DIR):
                raise SpecsError("Architecture specifications cannot be empty.")
            patch = staging.diff(baseline.commit, paths=(paths.FUNCTIONAL_SPECS_DIR, paths.ARCHITECTURE_SPECS_DIR))

        commit = specs.accept(patch, baseline)
        yield ai.ToolCallFinished(
            open_row.call_id, "Designed the project architecture" if cycle == 1 else open_row.label
        )
        return commit
