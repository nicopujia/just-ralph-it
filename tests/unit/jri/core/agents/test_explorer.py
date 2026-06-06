# pyright: reportArgumentType=false, reportInvalidCast=false
"""Tests for the context explorer adapter."""

import asyncio
from pathlib import Path

from jri.core.agents.explorer import Explorer
from tests.doubles.agents import FakeRunAgent


def test_explorer_run_uses_agent(tmp_path: Path) -> None:
    """Explorer delegates requests to its agent."""
    explorer = Explorer(model="test")
    fake_agent = FakeRunAgent("Summary:\n- ok")
    object.__setattr__(explorer, "agent", fake_agent)

    result = asyncio.run(
        explorer.run(project_root=tmp_path, request="Find tests.")
    )

    assert result == "Summary:\n- ok"
    assert fake_agent.requests == ["Find tests."]
