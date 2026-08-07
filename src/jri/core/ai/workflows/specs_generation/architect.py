from threading import Event
from typing import Literal

from openai.types.responses import ResponseInputParam
from pydantic import BaseModel

from jri.core import paths
from jri.core.ai import LLMRunner
from jri.core.settings import Settings
from jri.lib import prompt

type Result = Issues | Patch


class Input(BaseModel):
    functional_specs: str
    accepted_architecture: str
    tracked_repository_tree: list[str]
    explorer_report: str


class Issues(BaseModel):
    outcome: Literal["functional_specification_issues"]
    issues: list[str]


class Patch(BaseModel):
    outcome: Literal["architecture_patch"]
    patch: str


class Output(BaseModel):
    result: Issues | Patch


class Architect:
    FINAL_PROMPT = (
        "This is the final architecture pass. Return only an `architecture_patch`. Resolve every remaining\n"
        "architectural choice yourself while preserving the functional specifications exactly."
    )
    REPAIR_PROMPT = (
        "Git rejected the patch below. Return only an `architecture_patch` carrying the same intended change,\n"
        "rewritten so `git apply` accepts it against the accepted architecture. Hunks must not overlap, and\n"
        "every context line must match its file exactly."
    )

    def __init__(self, settings: Settings) -> None:
        profile = settings.agents.architect
        self.runner = LLMRunner(
            client=settings.llm.client,
            model=profile.model,
            reasoning_effort=profile.reasoning_effort,
            temperature=profile.temperature,
            prompt=(
                "Role: Software Architect.\n"
                "\n"
                "Goal: Define a stable, implementation-ready architecture for the supplied functional\n"
                "specifications and repository baseline.\n"
                "\n"
                "The product:\n"
                "    - The product you design is the user's, and the functional specifications are the\n"
                "      only source of its name, purpose, and scope.\n"
                "    - Name it exactly as they name it. When they give no name, refer to it generically\n"
                "      and never invent one.\n"
                "    - Never derive a product name, executable name, package name, or directory from\n"
                "      these instructions or from the paths they mention.\n"
                "    - The notebook and specification trees driving this task belong to the process\n"
                "      that produces the product. Wherever they surface in the repository, they are\n"
                "      never part of its architecture, naming, or layout.\n"
                "\n"
                "Authority and evidence:\n"
                "    - The functional specifications are the sole behavioral authority; decide purely\n"
                "      architectural questions yourself.\n"
                "    - The repository report and tracked tree are contextual evidence about the target\n"
                "      codebase.\n"
                "\n"
                "Output:\n"
                "    - Return `functional_specification_issues` when the functional specifications\n"
                "      contradict themselves, omit behavior required for implementation, or leave a\n"
                "      behavioral choice to the implementer.\n"
                "    - Report every such issue found in the pass, not only the first. Each set you\n"
                "      return costs a full re-analysis, so an incomplete list is a defect even when\n"
                "      every issue in it is real.\n"
                "    - Otherwise return `architecture_patch` containing a standard Git unified diff\n"
                "      against the supplied accepted architecture. Restrict the patch to Markdown files\n"
                f"      under `{paths.ARCHITECTURE_SPECS_ROOT}/`.\n"
                "    - Architecture must be concrete enough to guide implementation without redefining\n"
                "      product behavior."
            ),
        )

    def design(self, context: Input, cancelled: Event) -> Result | None:
        output = self.runner.parse(self._build_input(context, self.runner.prompt), Output, cancelled)
        return None if output is None else output.result

    def finish(self, context: Input, cancelled: Event) -> Patch | None:
        return self.runner.parse(
            self._build_input(context, f"{self.runner.prompt}\n\n{self.FINAL_PROMPT}"), Patch, cancelled
        )

    def repair(self, context: Input, patch: str, error: str, cancelled: Event) -> str | None:
        output = self.runner.parse(
            [
                *self._build_input(context, self.runner.prompt),
                # Instructions of ours are told apart from the quoted
                # data they are about, since a block is what the model
                # is told never to obey.
                {"role": "user", "content": self.REPAIR_PROMPT},
                {"role": "user", "content": prompt.render(rejected_patch=patch, git_error=error)},
            ],
            Patch,
            cancelled,
        )
        return None if output is None else output.patch

    @staticmethod
    def _build_input(context: Input, instructions: str) -> ResponseInputParam:
        return [
            {"role": "system", "content": instructions},
            {
                "role": "user",
                "content": prompt.render(
                    functional_specifications=context.functional_specs,
                    accepted_architecture=context.accepted_architecture,
                    tracked_repository_tree=context.tracked_repository_tree,
                    repository_analysis_report=context.explorer_report,
                ),
            },
        ]
