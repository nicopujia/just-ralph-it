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

logger = logging.getLogger(__name__)


class SpecsGen:
    MAX_CYCLES = 10

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.specs = Specs(settings.cwd)
        self.functional_analyst = functional_analyst.FunctionalAnalyst(settings)
        self.architect = architect.Architect(settings)

    def generate(
        self, active_commit: str | None
    ) -> Generator["ai.ToolCallStarted | ai.ToolCallFinished", None, SpecsResult]:
        baseline = self.specs.prepare(active_commit)
        explorer_report: str | None = None
        feedback: str | None = None
        rejected: str | None = None
        open_row = ai.ToolCallStarted("functional", "Writing functional specifications from your project notes", "✍️")

        for cycle in range(1, self.MAX_CYCLES + 1):
            logger.info("specs_cycle_started cycle=%d", cycle)
            if cycle == 1:
                yield open_row
            functional_result = self.functional_analyst.write(
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
                    accepted_specs=self.specs.render(baseline.functional),
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

            with self.specs.repository.open_worktree(baseline.commit) as staging:
                self.specs.apply(staging, functional_result.patch, paths.FUNCTIONAL_SPECS_ROOT)
                functional = self.specs.read(staging.path, paths.FUNCTIONAL_SPECS_DIR)
                if not functional:
                    raise SpecsError("Functional specifications cannot be empty.")

                if explorer_report is None:
                    yield ai.ToolCallStarted("explorer", "Studying your existing project", "🔎")
                    explorer = ai.Explorer(self.settings.model_copy(update={"cwd": staging.path}))
                    output: list[str] = []
                    for event in explorer.send_message(
                        "Study this repository generally. Report its structure, architecture, established patterns, "
                        "development commands, and the constraints that new work in it must respect."
                    ):
                        if isinstance(event, ai.ToolCallStarted):
                            output.clear()
                        elif isinstance(event, ai.TextDelta):
                            output.append(event.text)
                    explorer_report = "".join(output).strip()
                    if not explorer_report:
                        raise SpecsError("Repository exploration produced no report.")
                    yield ai.ToolCallFinished("explorer", "Studied your existing project")
                    open_row = ai.ToolCallStarted("architecture", "Designing the project architecture", "📐")
                    yield open_row

                context = architect.Input(
                    functional_specs=self.specs.render(functional),
                    accepted_architecture=self.specs.render(baseline.architecture),
                    tracked_tree="\n".join(
                        self.specs.repository.read_tracked_paths(baseline.commit)
                        if baseline.commit
                        else self.specs.repository.read_worktree_paths()
                    ),
                    explorer_report=explorer_report,
                )
                architecture_result = (
                    self.architect.finish(context) if cycle == self.MAX_CYCLES else self.architect.design(context)
                )
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
                    rejected = self.specs.render(functional)
                    feedback = "\n".join(f"- {issue}" for issue in architecture_result.issues)
                    continue

                self.specs.apply(staging, architecture_result.patch, paths.ARCHITECTURE_SPECS_ROOT)
                architecture = self.specs.read(staging.path, paths.ARCHITECTURE_SPECS_DIR)
                if not architecture:
                    raise SpecsError("Architecture specifications cannot be empty.")
                patch = staging.diff(baseline.commit, paths=(paths.FUNCTIONAL_SPECS_DIR, paths.ARCHITECTURE_SPECS_DIR))

            commit = self.specs.accept(patch, baseline)
            yield ai.ToolCallFinished(
                open_row.call_id, "Designed the project architecture" if cycle == 1 else open_row.label
            )
            return commit

        raise SpecsError("The final architecture cycle did not return a patch.")
