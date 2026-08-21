from collections.abc import Generator
from threading import Event

from pydantic import BaseModel

from jri.core import ai
from jri.core.ai.specs_writer import SpecsWriter
from jri.core.paths import FUNCTIONAL_SPECS_ROOT
from jri.core.settings import Settings
from jri.lib import git, prompt


class Input(BaseModel):
    notebook: str
    # A run with no accepted baseline has no earlier notebook to compare. It receives no diff and no rules for one.
    notebook_diff: str | None = None
    # A first pass writes the specifications that the project has none of. It receives no index and no rules for one.
    current_specs_index: str | None = None
    architect_feedback: list[str] | None = None


# A pass that must ask the user still keeps the files it wrote.
class Specifications(BaseModel):
    deleted_paths: list[str]
    unresolved: list[str]


class FunctionalAnalyst(SpecsWriter):
    READ_TOOL = "read_functional_specs"
    DIFF_PROMPT = ai.prompts.read("functional_analyst_diff")
    EXISTING_PROMPT = ai.prompts.read("functional_analyst_existing")
    FEEDBACK_PROMPT = ai.prompts.read("functional_analyst_feedback")

    # Send each set of rules only with the input that it is about. A run with no accepted baseline has no notebook
    # diff. A first pass has no specification index. A pass with no feedback has no round to answer.
    # Each set is about what this pass receives. Put it in the slot that the template keeps for it, above the
    # output rules and the constraint rules. A rule after those two sections reads as an addition to both.
    def __init__(
        self, settings: Settings, repository: git.Repository, *, changed: bool, existing: bool, feedback: bool
    ) -> None:
        rules = ((self.DIFF_PROMPT, changed), (self.EXISTING_PROMPT, existing), (self.FEEDBACK_PROMPT, feedback))
        super().__init__(
            client=settings.llm.client,
            profile=settings.agents.functional_analyst,
            prompt=ai.prompts.read(
                "functional_analyst",
                functional_specs_root=FUNCTIONAL_SPECS_ROOT,
                pass_rules="".join(f"\n{rule}\n" for rule, sent in rules if sent),
                call_rules=ai.prompts.read("specs_writer_calls", read_tool=self.READ_TOOL),
            ),
            repository=repository,
            specs_root=FUNCTIONAL_SPECS_ROOT,
            read_tool=self.READ_TOOL,
        )

    def write(
        self, context: Input, cancelled: Event
    ) -> Generator["ai.ReasoningDelta | ai.ToolCallStarted | ai.ToolCallFinished", None, Specifications | None]:
        return (yield from self.parse(self._render(context), Specifications, cancelled))

    @staticmethod
    def _render(context: Input) -> str:
        return prompt.render(
            current_notebook=context.notebook,
            notebook_diff_from_accepted_baseline=context.notebook_diff,
            current_functional_specifications_index=context.current_specs_index,
            architect_feedback=context.architect_feedback,
        )
