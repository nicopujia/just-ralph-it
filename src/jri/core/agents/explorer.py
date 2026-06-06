"""Read-only context explorer agent."""

from pathlib import Path

from pydantic_ai import Agent

from jri.core.agents.prompts import BASE_EXPLORER_PROMPT
from jri.core.tools.explorer import ExplorerDeps, build_explorer_tools


class Explorer:
    """Read-only context-gathering subagent."""

    def __init__(self, *, model: str) -> None:
        self.agent: Agent[ExplorerDeps, str] = Agent(
            model,
            deps_type=ExplorerDeps,
            instructions=BASE_EXPLORER_PROMPT,
            tools=build_explorer_tools(),
            end_strategy="exhaustive",
        )

    async def run(self, *, project_root: Path, request: str) -> str:
        """Run a read-only exploration request."""
        result = await self.agent.run(
            request,
            deps=ExplorerDeps(project_root=project_root),
        )
        return result.output
