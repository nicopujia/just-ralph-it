from collections.abc import Generator
from threading import Event
from typing import Literal

from pydantic import BaseModel

from jri.core import ai
from jri.core.ai.specs_writer import SpecsWriter
from jri.core.ai.tool import tool
from jri.core.paths import ARCHITECTURE_SPECS_ROOT, WORKSPACE_DIR
from jri.core.settings import Settings
from jri.lib import git, prompt

type Result = Issues | Architecture


class Input(BaseModel):
    functional_specs_index: str
    current_architecture_index: str
    explorer_report: str


# A pass that sends the functional specifications back keeps the architecture files it already wrote. Those two
# facts are not alternatives: the run saves that work and the next cycle designs on top of it.
class Issues(BaseModel):
    outcome: Literal["functional_specification_issues"]
    issues: list[str]


class Architecture(BaseModel):
    outcome: Literal["architecture"]
    deleted_paths: list[str]


class Output(BaseModel):
    result: Issues | Architecture


class Architect(SpecsWriter):
    # Both cycles read the same instructions and differ in one output rule. The last cycle takes the decisions
    # that a review would send back, so it always returns an architecture. Every other cycle can send functional
    # specification issues back instead.
    PROMPT = ai.prompts.read(
        "architect",
        architecture_specs_root=ARCHITECTURE_SPECS_ROOT,
        workspace_dir=WORKSPACE_DIR,
        pass_rule=ai.prompts.read("architect_issues"),
        call_rules=ai.prompts.read("specs_writer_calls", read_tool="read_architecture_specs"),
    )
    FINAL_PROMPT = ai.prompts.read(
        "architect",
        architecture_specs_root=ARCHITECTURE_SPECS_ROOT,
        workspace_dir=WORKSPACE_DIR,
        pass_rule=ai.prompts.read("architect_final"),
        call_rules=ai.prompts.read("specs_writer_calls", read_tool="read_architecture_specs"),
    )

    def __init__(self, settings: Settings, repository: git.Repository, *, final: bool) -> None:
        self._final = final
        super().__init__(
            client=settings.llm.client,
            profile=settings.agents.architect,
            prompt=self.FINAL_PROMPT if final else self.PROMPT,
            repository=repository,
            specs_root=ARCHITECTURE_SPECS_ROOT,
            read_tool=self.read_architecture_specs.__name__,
        )

    def design(
        self, context: Input, cancelled: Event
    ) -> Generator["ai.ReasoningDelta | ai.ToolCallStarted | ai.ToolCallFinished", None, Result | None]:
        message = prompt.render(
            functional_specifications_index=context.functional_specs_index,
            current_architecture_index=context.current_architecture_index,
            repository_analysis_report=context.explorer_report,
        )
        if self._final:
            return (yield from self.parse(message, Architecture, cancelled))
        output = yield from self.parse(message, Output, cancelled)
        return None if output is None else output.result

    @tool(
        "Read the full, current body of existing architecture specification files, named as the index shows them.",
        started_label="Reading {paths}",
        finished_label="Read {paths}",
        symbol="📖",
        replayed=False,
    )
    def read_architecture_specs(self, paths: list[str]) -> str:
        return self.read_specs(paths, ARCHITECTURE_SPECS_ROOT)
