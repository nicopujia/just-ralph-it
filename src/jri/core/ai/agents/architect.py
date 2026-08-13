from collections.abc import Generator
from threading import Event
from typing import Literal

from pydantic import BaseModel

from jri.core import ai
from jri.core.paths import (
    ARCHITECTURE_SPECS_DIR,
    ARCHITECTURE_SPECS_ROOT,
    FUNCTIONAL_SPECS_DIR,
    SPECS_DIR,
    WORKSPACE_DIR,
)
from jri.core.settings import Settings
from jri.core.specs import File, Specs
from jri.lib import git, prompt

from .base import Agent, tool

type Result = Issues | Architecture


class Input(BaseModel):
    functional_specs_index: str
    current_architecture_index: str
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


class Architect(Agent):
    PROMPT = ai.prompts.read("architect", architecture_specs_root=ARCHITECTURE_SPECS_ROOT, workspace_dir=WORKSPACE_DIR)
    # The last cycle takes the decisions that a review would send back, so it receives its own instructions and
    # always returns an architecture. Every other cycle can send functional specification issues back instead.
    FINAL_PROMPT = ai.prompts.read(
        "architect_final", architecture_specs_root=ARCHITECTURE_SPECS_ROOT, workspace_dir=WORKSPACE_DIR
    )

    def __init__(self, settings: Settings, repository: git.Repository, *, final: bool) -> None:
        self.repository = repository
        self._final = final
        profile = settings.agents.architect
        super().__init__(
            client=settings.llm.client,
            model=profile.model,
            reasoning_effort=profile.reasoning_effort,
            temperature=profile.temperature,
            prompt=self.FINAL_PROMPT if final else self.PROMPT,
        )

    def design(
        self, context: Input, cancelled: Event
    ) -> Generator["ai.ReasoningDelta | ai.ToolCallStarted | ai.ToolCallFinished", None, Result | None]:
        message = _render_message(context)
        if self._final:
            return (yield from self.parse(message, Architecture, cancelled))
        output = yield from self.parse(message, Output, cancelled)
        return None if output is None else output.result

    @tool(
        "Read the full, current body of existing functional specification files, named as the index shows them.",
        started_label="Reading {paths}",
        finished_label="Read {paths}",
        symbol="📖",
        replayed=False,
    )
    def read_functional_specs(self, paths: list[str]) -> str:
        return _read_selected(self.repository, FUNCTIONAL_SPECS_DIR, paths, "functional")

    @tool(
        "Read the full, current body of existing architecture specification files, named as the index shows them.",
        started_label="Reading {paths}",
        finished_label="Read {paths}",
        symbol="📖",
        replayed=False,
    )
    def read_architecture_specs(self, paths: list[str]) -> str:
        return _read_selected(self.repository, ARCHITECTURE_SPECS_DIR, paths, "architecture")


def _read_selected(repository: git.Repository, directory: str, requested: list[str], label: str) -> str:
    found = Specs.read(repository, directory, selected=requested)
    missing = sorted(set(requested) - {path.removeprefix(f"{SPECS_DIR}/") for path in found})
    if missing:
        raise RuntimeError(f"Could not find these {label} specifications: {', '.join(missing)}.")
    return Specs.render(found)


def _render_message(context: Input) -> str:
    return prompt.render(
        functional_specifications_index=context.functional_specs_index,
        current_architecture_index=context.current_architecture_index,
        tracked_repository_tree=context.tracked_repository_tree,
        repository_analysis_report=context.explorer_report,
    )
