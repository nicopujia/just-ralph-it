from typing import Literal

from pydantic import BaseModel

from jri.core import paths
from jri.core.ai import LLMRunner
from jri.core.settings import Settings

type Result = Ambiguities | Patch


class Input(BaseModel):
    """Input for producing or revising functional specifications."""

    notebook: str
    notebook_diff: str
    accepted_specs: str
    rejected_specs: str | None = None
    architect_feedback: str | None = None


class Ambiguities(BaseModel):
    """Behavioral questions that prevent specification generation."""

    outcome: Literal["ambiguities"]
    ambiguities: list[str]


class Patch(BaseModel):
    """A diff that produces the functional specification tree."""

    outcome: Literal["specification_patch"]
    patch: str


class Output(BaseModel):
    """Structured Functional Analyst response envelope."""

    result: Ambiguities | Patch


class FunctionalAnalyst:
    """Transform project knowledge into behavioral specifications."""

    def __init__(self, settings: Settings) -> None:
        agent = settings.agents.functional_analyst
        self.runner = LLMRunner(
            client=settings.llm.client,
            model=agent.model,
            reasoning_effort=agent.reasoning_effort,
            temperature=agent.temperature,
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
                    - Report every contradiction and material behavioral ambiguity found in the pass,
                      not only the first.
                    - Make a behavioral decision only where the notebook explicitly delegates that
                      domain or exact decision.
                    - Raise materially different behavioral alternatives as ambiguities, even inside a
                      delegated domain.
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

    def write(self, context: Input) -> Result:
        """Write specifications or report ambiguities.

        Returns:
            A specification patch or all blocking ambiguities.
        """

        revision = ""
        if context.rejected_specs is not None:
            revision = (
                "\n\nRejected functional draft:\n"
                f"{context.rejected_specs}\n\nArchitect feedback:\n{context.architect_feedback or ''}"
            )
        output = self.runner.parse(
            [
                {"role": "system", "content": self.runner.prompt},
                {
                    "role": "user",
                    "content": (
                        f"Current notebook:\n{context.notebook}\n\n"
                        f"Notebook diff from accepted baseline:\n{context.notebook_diff}\n\n"
                        f"Accepted functional specifications:\n{context.accepted_specs}"
                        f"{revision}"
                    ),
                },
            ],
            Output,
        )
        return output.result
