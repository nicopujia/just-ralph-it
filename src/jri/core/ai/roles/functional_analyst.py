from collections.abc import Generator
from threading import Event

from pydantic import BaseModel

from jri.core import ai
from jri.core.ai import Agent, tool
from jri.core.paths import FUNCTIONAL_SPECS_ROOT
from jri.core.settings import Settings
from jri.core.specs import File, Specs
from jri.lib import git, prompt


class Input(BaseModel):
    notebook: str
    # A run with no accepted baseline has no earlier notebook to compare. It receives no diff and no rules for one.
    notebook_diff: str | None = None
    # A first pass writes the specifications that the project has none of. It receives no index and no rules for one.
    current_specs_index: str | None = None
    architect_feedback: list[str] | None = None


# This is one pass over the notebook. It carries the specifications it settles and the decisions it cannot take.
# A pass that carries no file states why under `unresolved`. Those two facts are not alternatives:
# a pass that must ask the user still keeps the files it could write.
class Specifications(BaseModel):
    files: list[File]
    deleted_paths: list[str]
    unresolved: list[str]


class FunctionalAnalyst(Agent):
    DIFF_PROMPT = ai.prompts.read("functional_analyst_diff")
    EXISTING_PROMPT = ai.prompts.read("functional_analyst_existing")
    FEEDBACK_PROMPT = ai.prompts.read("functional_analyst_feedback")

    # Send each set of rules only with the input it speaks about. A run with no accepted baseline has no notebook diff,
    # a first pass has no specification index, and a pass with no feedback has no round to answer.
    # Each set speaks about what this pass receives, so it goes in the slot the template keeps for it, above the
    # output and constraint rules. A rule that arrives after those sections reads as an afterthought to both.
    def __init__(
        self, settings: Settings, repository: git.Repository, *, changed: bool, existing: bool, feedback: bool
    ) -> None:
        self.repository = repository
        rules = ((self.DIFF_PROMPT, changed), (self.EXISTING_PROMPT, existing), (self.FEEDBACK_PROMPT, feedback))
        super().__init__(
            client=settings.llm.client,
            profile=settings.agents.functional_analyst,
            prompt=ai.prompts.read(
                "functional_analyst",
                functional_specs_root=FUNCTIONAL_SPECS_ROOT,
                pass_rules="".join(f"\n{rule}\n" for rule, sent in rules if sent),
            ),
        )

    def write(
        self, context: Input, cancelled: Event
    ) -> Generator["ai.ReasoningDelta | ai.ToolCallStarted | ai.ToolCallFinished", None, Specifications | None]:
        message = prompt.render(
            current_notebook=context.notebook,
            notebook_diff_from_accepted_baseline=context.notebook_diff,
            current_functional_specifications_index=context.current_specs_index,
            architect_feedback=context.architect_feedback,
        )
        return (yield from self.parse(message, Specifications, cancelled))

    @tool(
        "Read the full, current body of existing functional specification files, named as the index shows them.",
        started_label="Reading {paths}",
        finished_label="Read {paths}",
        symbol="📖",
        replayed=False,
    )
    def read_functional_specs(self, paths: list[str]) -> str:
        return Specs.read_selected(self.repository, FUNCTIONAL_SPECS_ROOT, paths)
