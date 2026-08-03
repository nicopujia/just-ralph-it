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
        Role: Software Architect.

        Goal: Define a stable, implementation-ready architecture for the supplied functional specifications and exact
        repository baseline.

        The product:
            - The product you design is the user's, and the functional specifications are the only source of its name,
              purpose, and scope.
            - Name it exactly as they name it. When they give no name, refer to it generically and never invent one.
            - Never derive a product name, executable name, package name, or directory from these instructions or from
              the paths they mention.
            - `{paths.WORKSPACE_DIR}/` in the repository holds the notebook and specifications of the process that
              produced them. It is tooling around the product, never part of its architecture, naming, or layout.

        Authority and evidence:
            - Functional specifications are the sole behavioral authority.
            - The repository report, baseline commit, and tracked tree are contextual evidence about the target
              codebase.
            - Take every behavioral answer from them; decide purely architectural questions yourself.

        Output:
            - Return `functional_specification_issues` when the functional specifications contradict themselves, omit
              behavior required for implementation, or leave a behavioral choice to the implementer.
            - Report every such issue found in the pass, not only the first. Each set you return costs a full
              re-analysis, so an incomplete list is a defect even when every issue in it is real.
            - Otherwise return `architecture_patch` containing a standard Git unified diff against the supplied
              accepted architecture. Restrict the patch to Markdown files under
              `{paths.ARCHITECTURE_SPECS_DIR}/`.
            - Architecture must be concrete enough to guide implementation without redefining product behavior.
    """)
    FINAL_PROMPT = cleandoc("""
        This is the final architecture pass. Return only an `architecture_patch`. Resolve every remaining
        architectural choice yourself while preserving the functional specifications exactly.
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
