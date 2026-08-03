import shutil
import subprocess
from pathlib import Path

from jri.lib import git


def run_git(path: Path, *arguments: str) -> str:
    """Run a Git command inside a worktree.

    Returns:
        The command's trimmed standard output.
    """

    executable = shutil.which("git")
    assert executable is not None
    return subprocess.run(
        [executable, "-C", str(path), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


def create_repository(path: Path) -> git.Repository:
    """Create a repository holding a single committed file.

    Returns:
        The created repository.
    """

    path.mkdir(exist_ok=True)
    run_git(path, "init", "-q")
    (path / "README.md").write_text("# Project\n")
    run_git(path, "add", "README.md")
    run_git(path, "commit", "-qm", "initial")
    return git.Repository(path)
