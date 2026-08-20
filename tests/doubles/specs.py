from collections.abc import Mapping
from pathlib import Path

from jri.core.paths import SPECS_DIR


# Specification files as an earlier pass left them in the project, named the way a model names them.
def install_specifications(repository_path: Path, files: Mapping[str, str]) -> None:
    for path, content in files.items():
        specification = repository_path / SPECS_DIR / path
        specification.parent.mkdir(parents=True, exist_ok=True)
        specification.write_text(content, encoding="utf-8", newline="")
