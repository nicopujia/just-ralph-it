from inspect import cleandoc
from typing import Literal

from openai.types.responses import ResponseInputParam
from pydantic import BaseModel

from jri.core import paths
from jri.core.ai import LLMRunner
from jri.core.settings import Settings


class Input(BaseModel):
    """Input for designing an architecture for accepted behavior."""

    functional_specs: str
    accepted_architecture: str
    baseline_commit: str
    tracked_tree: str
    explorer_report: str


class Issues(BaseModel):
    """Functional issues that require another analysis pass."""

    outcome: Literal["functional_specification_issues"]
    issues: list[str]


class Patch(BaseModel):
    """A diff that produces the architecture specification tree."""

    outcome: Literal["architecture_patch"]
    patch: str


type Result = Issues | Patch


class Output(BaseModel):
    """Architect response envelope for issue-capable cycles."""

    result: Issues | Patch


class Architect(LLMRunner):
    """Transform functional specifications into architecture."""

    PROMPT = cleandoc(f"""
        Role: Software Architect for Just Ralph It (JRI).

        Goal: Define a stable, implementation-ready architecture for the supplied functional specifications and exact
        repository baseline.

        Authority and evidence:
            - Functional specifications are the sole behavioral authority.
            - The repository report, baseline commit, and tracked tree are contextual evidence about the target
              codebase.
            - Do not invent, change, or choose product behavior.

        Output:
            - Return `functional_specification_issues` when the functional specifications contradict themselves, omit
              behavior required for implementation, or leave a behavioral choice to the implementer.
            - Otherwise return `architecture_patch` containing a standard Git unified diff against the supplied
              accepted architecture. Restrict the patch to Markdown files under
              `{paths.ARCHITECTURE_SPECS_DIR}/`.
            - Architecture must be concrete enough to guide implementation without redefining product behavior.
    """)
    FINAL_PROMPT = cleandoc(f"""
        This is the final architecture pass. Return only an `architecture_patch`. Resolve any remaining architectural
        choices yourself while preserving the functional specifications exactly. The patch must still be a standard
        Git unified diff against the supplied accepted architecture and affect only Markdown files under
        `{paths.ARCHITECTURE_SPECS_DIR}/`.
    """)

    def __init__(self, settings: Settings) -> None:
        agent = settings.agents.architect
        super().__init__(
            client=settings.llm.client,
            model=agent.model,
            reasoning_effort=agent.reasoning_effort,
            temperature=agent.temperature,
        )

    def design(self, context: Input) -> Result:
        """Design architecture or report functional issues.

        Returns:
            An architecture patch or issues for the Functional Analyst.
        """

        output = self.parse(self._build_input(context, self.PROMPT), Output)
        return output.result

    def finish(self, context: Input) -> Patch:
        """Produce the architecture patch on the final pass.

        Returns:
            The required architecture patch.
        """

        return self.parse(self._build_input(context, f"{self.PROMPT}\n\n{self.FINAL_PROMPT}"), Patch)

    @staticmethod
    def _build_input(context: Input, prompt: str) -> ResponseInputParam:
        return [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": (
                    f"Functional specifications:\n{context.functional_specs}\n\n"
                    f"Accepted architecture:\n{context.accepted_architecture}\n\n"
                    f"Repository baseline commit:\n{context.baseline_commit}\n\n"
                    f"Tracked repository tree:\n{context.tracked_tree}\n\n"
                    f"Repository analysis report:\n{context.explorer_report}"
                ),
            },
        ]
