from collections.abc import Generator
from threading import Event

from pydantic import BaseModel

from jri.core import ai
from jri.core.paths import FUNCTIONAL_SPECS_DIR, FUNCTIONAL_SPECS_ROOT, SPECS_DIR
from jri.core.settings import Settings
from jri.core.specs import File, Specs
from jri.lib import git, prompt

from .base import Agent, tool


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
    PROMPT = ai.prompts.read("functional_analyst", functional_specs_root=FUNCTIONAL_SPECS_ROOT)
    DIFF_PROMPT = ai.prompts.read("functional_analyst_diff")
    EXISTING_PROMPT = ai.prompts.read("functional_analyst_existing")
    FEEDBACK_PROMPT = ai.prompts.read("functional_analyst_feedback")

    # Send each set of rules only with the input it speaks about. A run with no accepted baseline has no notebook diff,
    # a first pass has no specification index, and a pass with no feedback has no round to answer.
    def __init__(
        self, settings: Settings, repository: git.Repository, *, changed: bool, existing: bool, feedback: bool
    ) -> None:
        self.repository = repository
        instructions = [self.PROMPT]
        if changed:
            instructions.append(self.DIFF_PROMPT)
        if existing:
            instructions.append(self.EXISTING_PROMPT)
        if feedback:
            instructions.append(self.FEEDBACK_PROMPT)
        profile = settings.agents.functional_analyst
        super().__init__(
            client=settings.llm.client,
            model=profile.model,
            reasoning_effort=profile.reasoning_effort,
            temperature=profile.temperature,
            prompt="\n\n".join(instructions),
        )

    def write(
        self, context: Input, cancelled: Event
    ) -> Generator["ai.ReasoningDelta | ai.ToolCallStarted | ai.ToolCallFinished", None, Specifications | None]:
        return (yield from self.parse(_render_message(context), Specifications, cancelled))

    @tool(
        "Read the full, current body of existing functional specification files, named as the index shows them.",
        started_label="Reading {paths}",
        finished_label="Read {paths}",
        symbol="📖",
        replayed=False,
    )
    def read_functional_specs(self, paths: list[str]) -> str:
        found = Specs.read(self.repository, FUNCTIONAL_SPECS_DIR, selected=paths)
        missing = sorted(set(paths) - {path.removeprefix(f"{SPECS_DIR}/") for path in found})
        if missing:
            raise RuntimeError(f"Could not find these functional specifications: {', '.join(missing)}.")
        return Specs.render(found)


def _render_message(context: Input) -> str:
    return prompt.render(
        current_notebook=context.notebook,
        notebook_diff_from_accepted_baseline=context.notebook_diff,
        current_functional_specifications_index=context.current_specs_index,
        architect_feedback=context.architect_feedback,
    )
