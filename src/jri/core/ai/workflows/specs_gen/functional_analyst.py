from inspect import cleandoc
from typing import Literal

from pydantic import BaseModel

from jri.core import paths
from jri.core.ai import LLMRunner
from jri.core.settings import Settings


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


type Result = Ambiguities | Patch


class Output(BaseModel):
    """Structured Functional Analyst response envelope."""

    result: Ambiguities | Patch


class FunctionalAnalyst(LLMRunner):
    """Transform project knowledge into behavioral specifications."""

    PROMPT = cleandoc(f"""
        Role: Functional Analyst for Just Ralph It (JRI).

        Goal: Convert the complete project notebook into precise, testable behavioral specifications.

        Output:
            - Return `ambiguities` when any unresolved behavioral decision blocks a single faithful implementation.
            - Otherwise return `specification_patch` containing a standard Git unified diff against the supplied
              accepted functional specifications. Restrict the patch to Markdown files under
              `{paths.FUNCTIONAL_SPECS_DIR}/`.

        Behavioral authority:
            - The complete current notebook is authoritative.
            - Report every contradiction and material behavioral ambiguity found in the pass, not only the first.
            - Make a behavioral decision only where the notebook explicitly delegates that domain or exact decision.
            - Never use delegation to choose between materially different behavioral alternatives.
            - State every delegated decision explicitly and testably in the specifications.
            - Architecture, code organization, dependencies, and implementation mechanics are out of scope.

        Revision rules:
            - When Architect feedback is supplied, resolve it against the whole notebook and its delegated authority.
            - The rejected draft is context only. Produce a complete replacement patch from the accepted baseline.
            - Escalate feedback as ambiguities when it requires user authority, exposes contradictory requirements,
              or has materially different behavioral solutions.
    """)

    def __init__(self, settings: Settings) -> None:
        agent = settings.agents.functional_analyst
        super().__init__(
            client=settings.llm.client,
            model=agent.model,
            reasoning_effort=agent.reasoning_effort,
            temperature=agent.temperature,
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
        output = self.parse(
            [
                {"role": "system", "content": self.PROMPT},
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
