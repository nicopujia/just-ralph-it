import json
from functools import cache
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath, PureWindowsPath
from typing import cast

_PACKAGE = "jri.core.agents.bundle"
_MANIFEST_NAME = "manifest.json"


@cache
def resource_manifest() -> dict[str, str]:
    payload = files(_PACKAGE).joinpath(_MANIFEST_NAME).read_text(encoding="utf-8")
    manifest: object = json.loads(payload)
    if not isinstance(manifest, dict):
        raise ValueError("agent resource manifest must be an object")

    resources: dict[str, str] = {}
    for resource_id, resource_path in cast(dict[object, object], manifest).items():
        if not isinstance(resource_id, str) or not resource_id:
            raise ValueError("agent resource manifest IDs must be non-empty strings")
        if not isinstance(resource_path, str):
            raise ValueError(f"agent resource '{resource_id}' path must be a string")
        resources[resource_id] = _validate_manifest_path(resource_id, resource_path)
    return resources


def resource_relative_path(resource_id: str) -> str:
    try:
        return resource_manifest()[resource_id]
    except KeyError as exc:
        raise ValueError(f"unknown agent resource ID: {resource_id}") from exc


def resource_path(resource_id: str) -> Traversable:
    relative_path = resource_relative_path(resource_id)
    return files(_PACKAGE).joinpath(*PurePosixPath(relative_path).parts)


def _validate_manifest_path(resource_id: str, raw_path: str) -> str:
    path = PurePosixPath(raw_path)
    if "\0" in raw_path or "\\" in raw_path or raw_path != path.as_posix():
        raise ValueError(f"agent resource '{resource_id}' path must be a POSIX path")
    if path.is_absolute() or PureWindowsPath(raw_path).is_absolute() or not path.parts:
        raise ValueError(f"agent resource '{resource_id}' path must be relative")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(
            f"agent resource '{resource_id}' path must not traverse parents"
        )
    return path.as_posix()
