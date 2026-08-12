from collections.abc import Generator
from threading import Event
from typing import Literal

from openai.types.responses import ResponseInputParam
from pydantic import BaseModel

from jri.core import paths
from jri.core.ai import LLMRunner, ReasoningDelta, prompts
from jri.core.settings import Settings
from jri.lib import prompt

type Result = Issues | Architecture


class File(BaseModel):
    path: str
    content: str


class Input(BaseModel):
    functional_specs: str
    current_architecture: str
    tracked_repository_tree: list[str]
    explorer_report: str


class Issues(BaseModel):
    outcome: Literal["functional_specification_issues"]
    issues: list[str]


class Architecture(BaseModel):
    outcome: Literal["architecture"]
    files: list[File]
    deleted_paths: list[str]


class Output(BaseModel):
    result: Issues | Architecture


class Architect:
    FINAL_PROMPT = prompts.read("architect_final")

    def __init__(self, settings: Settings) -> None:
        profile = settings.agents.architect
        self.runner = LLMRunner(
            client=settings.llm.client,
            model=profile.model,
            reasoning_effort=profile.reasoning_effort,
            temperature=profile.temperature,
            prompt=prompts.read(
                "architect", architecture_specs_root=paths.ARCHITECTURE_SPECS_ROOT, workspace_dir=paths.WORKSPACE_DIR
            ),
        )

    def design(self, context: Input, cancelled: Event) -> Generator[ReasoningDelta, None, Result | None]:
        output = yield from self.runner.parse(self._build_input(context, self.runner.prompt), Output, cancelled)
        return None if output is None else output.result

    def finish(self, context: Input, cancelled: Event) -> Generator[ReasoningDelta, None, Architecture | None]:
        return (
            yield from self.runner.parse(
                self._build_input(context, f"{self.runner.prompt}\n\n{self.FINAL_PROMPT}"), Architecture, cancelled
            )
        )

    @staticmethod
    def _build_input(context: Input, instructions: str) -> ResponseInputParam:
        return [
            {"role": "system", "content": instructions},
            {
                "role": "user",
                "content": prompt.render(
                    functional_specifications=context.functional_specs,
                    current_architecture=context.current_architecture,
                    tracked_repository_tree=context.tracked_repository_tree,
                    repository_analysis_report=context.explorer_report,
                ),
            },
        ]
