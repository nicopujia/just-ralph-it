"""Git operations for JRI-owned files."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

COMMIT_MESSAGE = "docs: capture JRI specs"
COMMITTED_MESSAGE = "Committed .jri changes."
JRI_COMMIT_EMAIL = "jri@localhost"
JRI_COMMIT_NAME = "Just Ralph It"


@dataclass(frozen=True)
class CommitResult:
    """Result of committing JRI files."""

    committed: bool
    message: str


class GitError(RuntimeError):
    """Raised when a required Git operation fails."""


def commit_jri_files(project_root: Path) -> CommitResult:
    """Commit only committable JRI files if they changed."""
    pathspecs = _find_committable_paths(project_root)
    if not pathspecs:
        return CommitResult(
            committed=False, message="No .jri changes to commit."
        )

    status = _run_git(
        project_root,
        "status",
        "--porcelain",
        "--",
        *pathspecs,
    )
    if not status.stdout.strip():
        return CommitResult(
            committed=False, message="No .jri changes to commit."
        )

    _run_git(project_root, "add", "--", *pathspecs)
    _run_git(
        project_root,
        "-c",
        f"user.email={JRI_COMMIT_EMAIL}",
        "-c",
        f"user.name={JRI_COMMIT_NAME}",
        "commit",
        "--quiet",
        "-m",
        COMMIT_MESSAGE,
        "--",
        *pathspecs,
    )
    return CommitResult(committed=True, message=COMMITTED_MESSAGE)


def _find_committable_paths(project_root: Path) -> list[str]:
    candidates = [
        project_root / ".jri" / ".gitignore",
        project_root / ".jri" / "scratchpad.md",
        *(project_root / ".jri" / "specs").glob("**/*.md"),
    ]
    existing = [
        str(path.relative_to(project_root))
        for path in candidates
        if path.exists()
    ]
    return list(
        dict.fromkeys([*existing, *_find_tracked_deleted_paths(project_root)])
    )


def _find_tracked_deleted_paths(project_root: Path) -> list[str]:
    deleted = _run_git(
        project_root,
        "ls-files",
        "--deleted",
        "--",
        ".jri/.gitignore",
        ".jri/scratchpad.md",
        ".jri/specs",
    )
    return [
        path
        for path in deleted.stdout.splitlines()
        if _is_committable_jri_path(path)
    ]


def _is_committable_jri_path(path: str) -> bool:
    return path in {".jri/.gitignore", ".jri/scratchpad.md"} or (
        path.startswith(".jri/specs/") and path.endswith(".md")
    )


def _run_git(
    project_root: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise GitError(detail)
    return result
