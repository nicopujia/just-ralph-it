from inspect import cleandoc
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
    REPAIR_PROMPT = cleandoc("""
        Git rejected the patch below. Return only a `specification_patch` carrying the same intended change,
        rewritten so `git apply` accepts it against the accepted functional specifications. Hunks must not
        overlap, and every context line must match its file exactly.
    """)

    def __init__(self, settings: Settings) -> None:
        profile = settings.agents.functional_analyst
        self.runner = LLMRunner(
            client=settings.llm.client,
            model=profile.model,
            reasoning_effort=profile.reasoning_effort,
            temperature=profile.temperature,
            prompt=f"""
                Role: Functional Analyst.

                Goal: Convert the complete project notebook into precise, testable behavioral
                specifications.

                The product:
                    - The product you specify is the user's, and the notebook is the only source of
                      its name, purpose, and scope.
                    - Name it exactly as the notebook names it. When the notebook gives no name, refer
                      to it generically (e.g. "the application") and never invent one.
                    - Never take a product name, executable name, package name, or directory from
                      these instructions or from the paths they mention. The specification tree you
                      write into belongs to the process that produces the product, never to the
                      product itself.

                Output:
                    - Return `ambiguities` when any unresolved behavioral decision blocks a single
                      faithful implementation.
                    - Otherwise return `specification_patch` containing a standard Git unified diff
                      against the supplied accepted functional specifications. Restrict the patch to
                      Markdown files under `{paths.FUNCTIONAL_SPECS_ROOT}/`.

                Behavioral authority:
                    - The complete current notebook is authoritative. The notebook diff only shows
                      what changed since the accepted baseline; it never limits the scope of the
                      specifications.
                    - Report every contradiction in the notebook, and every ambiguity whose
                      alternatives the user would recognize as changing what the product does for
                      them, not only the first.
                    - Make a behavioral decision only where the notebook explicitly delegates that
                      domain or exact decision.
                    - Inside a delegated domain, decide and write the decision down: the delegation
                      exists so the user does not have to rule on what they would not notice. Escalate
                      there only where the alternatives change what the product does for them, judged
                      against the project the notebook describes rather than the hardest project its
                      words could describe.
                    - State every delegated decision explicitly and testably in the specifications.
                    - Architecture, code organization, dependencies, and implementation mechanics are
                      out of scope.

                Revision rules:
                    - When Architect feedback is supplied, resolve it against the whole notebook and
                      its delegated authority.
                    - The rejected draft is context only. Produce a complete replacement patch from
                      the accepted baseline.
                    - Escalate feedback as ambiguities when it requires user authority, exposes
                      contradictory requirements, or has materially different behavioral solutions.
            """,
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
