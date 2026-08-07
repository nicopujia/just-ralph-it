from threading import Event
from typing import Literal

from openai.types.responses import ResponseInputParam
from pydantic import BaseModel

from jri.core import paths
from jri.core.ai import LLMRunner
from jri.core.settings import Settings
from jri.lib import prompt

type Result = Ambiguities | Patch


class Input(BaseModel):
    notebook: str
    notebook_diff: str
    accepted_specs: str
    rejected_specs: str | None = None
    architect_feedback: list[str] | None = None


class Ambiguities(BaseModel):
    outcome: Literal["ambiguities"]
    ambiguities: list[str]


class Patch(BaseModel):
    outcome: Literal["specification_patch"]
    patch: str


class Output(BaseModel):
    result: Ambiguities | Patch


class FunctionalAnalyst:
    REPAIR_PROMPT = (
        "Git rejected the patch below. Return only a `specification_patch` carrying the same intended change,\n"
        "rewritten so `git apply` accepts it against the accepted functional specifications. Hunks must not\n"
        "overlap, and every context line must match its file exactly."
    )

    def __init__(self, settings: Settings) -> None:
        profile = settings.agents.functional_analyst
        self.runner = LLMRunner(
            client=settings.llm.client,
            model=profile.model,
            reasoning_effort=profile.reasoning_effort,
            temperature=profile.temperature,
            prompt=(
                "Role: Functional Analyst.\n"
                "\n"
                "Goal: Convert the complete project notebook into precise, testable behavioral\n"
                "specifications.\n"
                "\n"
                "The product:\n"
                "    - The product you specify is the user's, and the notebook is the only source of\n"
                "      its name, purpose, and scope.\n"
                "    - Name it exactly as the notebook names it. When the notebook gives no name, refer\n"
                '      to it generically (e.g. "the application") and never invent one.\n'
                "    - Never take a product name, executable name, package name, or directory from\n"
                "      these instructions or from the paths they mention. The specification tree you\n"
                "      write into belongs to the process that produces the product, never to the\n"
                "      product itself.\n"
                "\n"
                "Output:\n"
                "    - Return `ambiguities` when any unresolved behavioral decision blocks a single\n"
                "      faithful implementation and the notebook has not delegated it to you by name.\n"
                "    - Otherwise return `specification_patch` containing a standard Git unified diff\n"
                "      against the supplied accepted functional specifications. Restrict the patch to\n"
                f"      Markdown files under `{paths.FUNCTIONAL_SPECS_ROOT}/`.\n"
                "\n"
                "Behavioral authority:\n"
                "    - The complete current notebook is authoritative. The notebook diff only shows\n"
                "      what changed since the accepted baseline; it never limits the scope of the\n"
                "      specifications.\n"
                "    - Report every contradiction in the notebook, and every ambiguity whose\n"
                "      alternatives the user would recognize as changing what the product does for\n"
                "      them, not only the first.\n"
                "    - Make a behavioral decision only where the notebook explicitly delegates that\n"
                "      domain or exact decision.\n"
                "    - Inside a delegated domain, decide and write the decision down: the delegation\n"
                "      exists so the user does not have to rule on what they would not notice. Escalate\n"
                "      there only where the alternatives change what the product does for them, judged\n"
                "      against the project the notebook describes rather than the hardest project its\n"
                "      words could describe.\n"
                "    - State every delegated decision explicitly and testably in the specifications.\n"
                "    - Architecture, code organization, dependencies, and implementation mechanics are\n"
                "      out of scope.\n"
                "\n"
                "Revision rules:\n"
                "    - When Architect feedback is supplied, resolve it against the whole notebook and\n"
                "      its delegated authority.\n"
                "    - The rejected draft is context only. Produce a complete replacement patch from\n"
                "      the accepted baseline.\n"
                "    - Escalate feedback as ambiguities when it requires user authority, exposes\n"
                "      contradictory requirements, or has materially different behavioral solutions\n"
                "      whose choice the notebook has not delegated to you by name."
            ),
        )

    def write(self, context: Input, cancelled: Event) -> Result | None:
        output = self.runner.parse(self._build_input(context), Output, cancelled)
        return None if output is None else output.result

    def repair(self, context: Input, patch: str, error: str, cancelled: Event) -> str | None:
        output = self.runner.parse(
            [
                *self._build_input(context),
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

    def _build_input(self, context: Input) -> ResponseInputParam:
        return [
            {"role": "system", "content": self.runner.prompt},
            {
                "role": "user",
                "content": prompt.render(
                    current_notebook=context.notebook,
                    notebook_diff_from_accepted_baseline=context.notebook_diff,
                    accepted_functional_specifications=context.accepted_specs,
                    rejected_functional_draft=context.rejected_specs,
                    architect_feedback=context.architect_feedback,
                ),
            },
        ]
