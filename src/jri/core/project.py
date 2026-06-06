"""Project state discovery and initialization."""

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

SCRATCHPAD_TEMPLATE = """# Scratchpad

## Open Topics

## Pending Questions

## Notes
"""


@dataclass(frozen=True)
class ProjectState:
    """Resolved JRI project state."""

    root: Path

    @property
    def jri_dir(self) -> Path:
        """Return the active .jri directory."""
        return self.root / ".jri"


def find_project_root(start: Path) -> Path:
    """Find the project root that owns the active JRI state."""
    current = start.resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / ".jri").is_dir():
            return candidate

    result = subprocess.run(
        ["git", "-C", str(current), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return Path(result.stdout.strip()).resolve()

    return current


def initialize_project(start: Path, *, force: bool = False) -> ProjectState:
    """Initialize the active JRI project state."""
    root = find_project_root(start)
    _ensure_git_repository(root)

    jri_dir = root / ".jri"
    if force and jri_dir.exists():
        shutil.rmtree(jri_dir)

    (jri_dir / "specs").mkdir(parents=True, exist_ok=True)
    (jri_dir / "logs").mkdir(parents=True, exist_ok=True)
    _write_if_missing(jri_dir / ".gitignore", "logs/\n")
    _write_if_missing(jri_dir / "scratchpad.md", SCRATCHPAD_TEMPLATE)
    (jri_dir / "logs" / "interview.jsonl").touch(exist_ok=True)
    return ProjectState(root=root)


def _ensure_git_repository(root: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)


def _write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")
