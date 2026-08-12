from collections.abc import Generator
from threading import Event

from openai.types.responses import ResponseInputParam
from pydantic import BaseModel

from jri.core import paths
from jri.core.ai import LLMRunner, ReasoningDelta, prompts
from jri.core.settings import Settings
from jri.lib import prompt


class File(BaseModel):
    path: str
    content: str


class Input(BaseModel):
    notebook: str
    notebook_diff: str
    # A first pass writes the specifications that the project has none of. It receives no tree and no rules for one.
    current_specs: str | None = None
    architect_feedback: list[str] | None = None


# This is one pass over the notebook. It carries the specifications it settles and the decisions it cannot take.
# A pass that carries no file states why under `unresolved`. Those two facts are not alternatives:
# a pass that must ask the user still keeps the files it could write.
class Specifications(BaseModel):
    files: list[File]
    deleted_paths: list[str]
    unresolved: list[str]


class FunctionalAnalyst:
    EXISTING_PROMPT = prompts.read("functional_analyst_existing")
    FEEDBACK_PROMPT = prompts.read("functional_analyst_feedback")

    def __init__(self, settings: Settings) -> None:
        profile = settings.agents.functional_analyst
        self.runner = LLMRunner(
            client=settings.llm.client,
            model=profile.model,
            reasoning_effort=profile.reasoning_effort,
            temperature=profile.temperature,
            prompt=prompts.read("functional_analyst", functional_specs_root=paths.FUNCTIONAL_SPECS_ROOT),
        )

    # Send each set of rules only with the input it speaks about.
    # A first pass has no specification tree, and a pass with no feedback has no round to answer.
    def write(self, context: Input, cancelled: Event) -> Generator[ReasoningDelta, None, Specifications | None]:
        instructions = [self.runner.prompt]
        if context.current_specs is not None:
            instructions.append(self.EXISTING_PROMPT)
        if context.architect_feedback:
            instructions.append(self.FEEDBACK_PROMPT)
        return (
            yield from self.runner.parse(
                self._build_input(context, "\n\n".join(instructions)), Specifications, cancelled
            )
        )

    @staticmethod
    def _build_input(context: Input, instructions: str) -> ResponseInputParam:
        return [
            {"role": "system", "content": instructions},
            {
                "role": "user",
                "content": prompt.render(
                    current_notebook=context.notebook,
                    notebook_diff_from_accepted_baseline=context.notebook_diff,
                    current_functional_specifications=context.current_specs,
                    architect_feedback=context.architect_feedback,
                ),
            },
        ]
