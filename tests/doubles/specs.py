from collections.abc import Mapping
from pathlib import Path

from jri.core.paths import SPECS_DIR
from tests.doubles.openai import call, response


# Specification files as an earlier pass left them in the project, named the way a model names them.
def install_specifications(repository_path: Path, files: Mapping[str, str]) -> None:
    for path, content in files.items():
        specification = repository_path / SPECS_DIR / path
        specification.parent.mkdir(parents=True, exist_ok=True)
        specification.write_text(content, encoding="utf-8", newline="")


def summarize(path: str) -> str:
    return f"Specification for {path}."


# A pass writes its files with tool calls, and then returns what stays outside them.
def write_files(role: str, files: Mapping[str, str]) -> list[object]:
    if not files:
        return []
    written = [{"path": path, "content": content, "summary": summarize(path)} for path, content in files.items()]
    return [response(call(f"write-{role}", "write_specs", files=written))]
