from collections.abc import Generator
from threading import Event
from typing import Literal

from openai.types.responses import ResponseInputParam
from pydantic import BaseModel

from jri.core import paths
from jri.core.ai import LLMRunner, ReasoningDelta, prompts
from jri.core.settings import Settings
from jri.lib import prompt

type Result = Ambiguities | Specifications


class File(BaseModel):
    path: str
    content: str


class Input(BaseModel):
    notebook: str
    notebook_diff: str
    current_specs: str
    architect_feedback: list[str] | None = None


class Ambiguities(BaseModel):
    outcome: Literal["ambiguities"]
    ambiguities: list[str]


class Specifications(BaseModel):
    outcome: Literal["specifications"]
    files: list[File]
    deleted_paths: list[str]


class Output(BaseModel):
    result: Ambiguities | Specifications


class FunctionalAnalyst:
    def __init__(self, settings: Settings) -> None:
        profile = settings.agents.functional_analyst
        self.runner = LLMRunner(
            client=settings.llm.client,
            model=profile.model,
            reasoning_effort=profile.reasoning_effort,
            temperature=profile.temperature,
            prompt=prompts.read("functional_analyst", functional_specs_root=paths.FUNCTIONAL_SPECS_ROOT),
        )

    def write(self, context: Input, cancelled: Event) -> Generator[ReasoningDelta, None, Result | None]:
        output = yield from self.runner.parse(self._build_input(context), Output, cancelled)
        return None if output is None else output.result

    def _build_input(self, context: Input) -> ResponseInputParam:
        return [
            {"role": "system", "content": self.runner.prompt},
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
