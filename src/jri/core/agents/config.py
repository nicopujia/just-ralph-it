from importlib.resources import files
from pathlib import Path


def iter_bundle_assets() -> tuple[str, ...]:
    root = _resource_path("")
    return tuple(
        sorted(
            str(item.relative_to(root))
            for item in root.rglob("*")
            if item.is_file()
            and "__pycache__" not in item.parts
            and item.suffix != ".pyc"
        )
    )


def load_asset_text(name: str | Path) -> str:
    return _resource_path(name).read_text(encoding="utf-8")


def _resource_path(name: str | Path):
    return files("jri.core.agents.bundle").joinpath(*Path(name).parts)
