from importlib.resources import files
from pathlib import Path

COPYABLE_DIRECTORIES = ("agents", "tools")


def iter_directory_assets(directory: str) -> tuple[str, ...]:
    root = _resource_path(directory)
    return tuple(sorted(item.name for item in root.iterdir() if item.is_file()))


def load_asset_text(name: str | Path) -> str:
    return _resource_path(name).read_text(encoding="utf-8")


def _resource_path(name: str | Path):
    return files("jri.core.opencode").joinpath(*Path(name).parts)
