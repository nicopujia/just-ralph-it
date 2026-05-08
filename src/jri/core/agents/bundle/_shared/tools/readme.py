import json

from ._validation import (
    apply_exact_edits,
    assert_exact_edits,
    diff_text,
    repo_root_child,
)


def run_read_readme(_payload: dict[str, object]) -> str:
    readme_path = repo_root_child("README.md")
    if readme_path.is_symlink():
        raise ValueError("refusing to read symlinked README.md")
    if not readme_path.exists():
        raise ValueError("README.md does not exist")
    return readme_path.read_text(encoding="utf-8")


def run_edit_readme(payload: dict[str, object]) -> str:
    edits = assert_exact_edits(payload)
    readme_path = repo_root_child("README.md")
    if readme_path.is_symlink():
        raise ValueError("refusing to edit symlinked README.md")
    if not readme_path.exists():
        raise ValueError("README.md does not exist")
    if not readme_path.is_file():
        raise ValueError("README.md is not a regular file")

    source = readme_path.read_text(encoding="utf-8")
    updated, replacements = apply_exact_edits(source, edits)
    readme_path.write_text(updated, encoding="utf-8")
    result = {
        "path": "README.md",
        "replacements": replacements,
        "diff": diff_text("README.md", source, updated),
    }
    return json.dumps(result, indent=2) + "\n"
