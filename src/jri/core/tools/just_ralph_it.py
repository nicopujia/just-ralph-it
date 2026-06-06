"""Finalization tool for successful JRI interviews."""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from jri.core.git import commit_jri_files
from jri.core.triggers import is_trigger_message


@dataclass(frozen=True)
class JustRalphItResult:
    """Sentinel-style finalization result."""

    should_exit: bool
    message: str


class JustRalphItError(RuntimeError):
    """Raised when finalization is not allowed."""


async def finalize_jri(
    *,
    project_root: Path,
    latest_user_message: str,
    readiness_summary: str,
    known_blockers: list[str] | None = None,
) -> JustRalphItResult:
    """Commit finalized JRI files and signal the REPL to exit."""
    if not is_trigger_message(latest_user_message):
        msg = "just_ralph_it requires a trigger phrase."
        raise JustRalphItError(msg)
    if known_blockers:
        raise JustRalphItError("\n".join(known_blockers))
    if not _has_spec_files(project_root):
        msg = "just_ralph_it requires at least one persisted spec file."
        raise JustRalphItError(msg)

    commit = await asyncio.to_thread(commit_jri_files, project_root)
    detail = "committed" if commit.committed else "already up to date"
    return JustRalphItResult(
        should_exit=True,
        message=(
            f"Specs finalized and {detail}. Ralph is coming soon to JRI. "
            "For now, you need to figure out how to implement the specs "
            f"yourself. Readiness: {readiness_summary}"
        ),
    )


def _has_spec_files(project_root: Path) -> bool:
    specs_dir = project_root / ".jri" / "specs"
    return specs_dir.exists() and any(specs_dir.glob("**/*.md"))
