import json
from typing import Any

from .validation import (
    _apply_exact_edits,
    _assert_exact_edits,
    _diff_text,
    _repo_root_child,
)


def _run_read_readme(_payload: dict[str, Any]) -> str:
    readme_path = _repo_root_child("README.md")
    if readme_path.is_symlink():
        raise ValueError("refusing to read symlinked README.md")
    if not readme_path.exists():
        raise ValueError("README.md does not exist")
    return readme_path.read_text(encoding="utf-8")


def _run_edit_readme(payload: dict[str, Any]) -> str:
    edits = _assert_exact_edits(payload)
    readme_path = _repo_root_child("README.md")
    if readme_path.is_symlink():
        raise ValueError("refusing to edit symlinked README.md")
    if not readme_path.exists():
        raise ValueError("README.md does not exist")
    if not readme_path.is_file():
        raise ValueError("README.md is not a regular file")

    source = readme_path.read_text(encoding="utf-8")
    updated, replacements = _apply_exact_edits(source, edits)
    readme_path.write_text(updated, encoding="utf-8")
    result = {
        "path": "README.md",
        "replacements": replacements,
        "diff": _diff_text("README.md", source, updated),
    }
    return json.dumps(result, indent=2) + "\n"
