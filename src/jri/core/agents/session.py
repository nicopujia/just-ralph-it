import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from ..errors import JriError
from ..timeline import TimelineEvent, TimelineStore
from .client import AgentRuntime, PiRuntime
from .config import (
    COPYABLE_DIRECTORIES,
    COPYABLE_FILES,
    iter_directory_assets,
    load_asset_text,
)
from .resources import resource_relative_path


@contextmanager
def runtime_env(
    *,
    overrides: dict[str, str | None],
    config_name: str = "package.json",
    included_agents: set[str] | None = None,
) -> Iterator[dict[str, str]]:
    del config_name
    with tempfile.TemporaryDirectory(prefix="jri-pi-") as tmp_dir:
        bundle_root = Path(tmp_dir)
        for name in COPYABLE_FILES:
            (bundle_root / name).write_text(load_asset_text(name), encoding="utf-8")
        for directory in COPYABLE_DIRECTORIES:
            target_dir = bundle_root / directory
            target_dir.mkdir(parents=True, exist_ok=True)
            for name in iter_directory_assets(directory):
                if not _should_copy_agent_asset(
                    directory, Path(name), included_agents=included_agents
                ):
                    continue
                target_path = target_dir / name
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(
                    load_asset_text(Path(directory) / name), encoding="utf-8"
                )
        _write_package_manifest(bundle_root, overrides=overrides)
        pythonpath_entry = str(Path(__file__).resolve().parents[3])
        yield {
            "JRI_PI_PACKAGE": str(bundle_root.resolve()),
            "JRI_PYTHON": sys.executable,
            "JRI_PYTHONPATH": pythonpath_entry,
        }


def call_with_runtime(
    runtime: AgentRuntime,
    *,
    root: Path,
    operation: Callable[[], Any],
) -> Any:
    if isinstance(runtime, PiRuntime) and not runtime.is_healthy():
        with runtime_env(overrides={}) as env:
            runtime.start(env=env, cwd=root)
            try:
                return operation()
            finally:
                runtime.stop()
    return operation()


def list_sessions(
    runtime: AgentRuntime,
    *,
    root: Path,
) -> list[dict[str, object]]:
    if isinstance(runtime, PiRuntime):
        return runtime.list_sessions(root=root)
    return call_with_runtime(
        runtime, root=root, operation=lambda: runtime.list_sessions(root=root)
    )


def detect_latest_session(
    *,
    root: Path,
    before: set[str],
    sessions: list[dict[str, object]],
) -> str | None:
    for session in sessions:
        session_id = session.get("id")
        directory = session.get("directory")
        if isinstance(session_id, str) and isinstance(directory, str):
            if Path(directory).resolve() == root and session_id not in before:
                return session_id
    for session in sessions:
        session_id = session.get("id")
        directory = session.get("directory")
        if (
            isinstance(session_id, str)
            and isinstance(directory, str)
            and Path(directory).resolve() == root
        ):
            return session_id
    return None


def export_session_if_available(
    runtime: AgentRuntime,
    *,
    root: Path,
    destination_dir: Path,
    timeline: TimelineStore,
    session_id: str | None,
    task_slug: str | None = None,
) -> Path | None:
    if session_id is None:
        return None
    export_path = destination_dir / f"{session_id}.json"
    export_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        call_with_runtime(
            runtime,
            root=root,
            operation=lambda: runtime.export_session(session_id, export_path),
        )
    except JriError as exc:
        error_msg = f"Failed to export session {session_id}: {exc}"
        print(error_msg, file=sys.stderr)
        timeline.record(
            TimelineEvent(
                ts=TimelineStore.now_iso(),
                event="export_failed",
                task=task_slug,
                detail={
                    "session_id": session_id,
                    "error": str(exc),
                },
            )
        )
        return None
    return export_path


def _should_copy_agent_asset(
    directory: str, name: Path, *, included_agents: set[str] | None
) -> bool:
    if included_agents is None or directory == "tools":
        return True
    if directory in included_agents:
        return True
    return directory == "ralph" and (
        name.suffix == ".ts" or name.parts[:1] == ("skills",)
    )


def _write_package_manifest(
    bundle_root: Path, *, overrides: dict[str, str | None]
) -> None:
    package = {
        "name": "jri-pi-runtime",
        "private": True,
        "keywords": ["pi-package"],
        "pi": {
            "extensions": [_manifest_reference("extensions.default")],
            "skills": [_manifest_skill_root_reference("skills.hostedProjects")],
            "prompts": [_manifest_top_level_reference("prompts.interrogator")],
            "tools": [_manifest_top_level_reference("tools.pythonRunner")],
            "themes": [_manifest_top_level_reference("themes.modernYellow")],
        },
        "jri": {
            "models": {name: model for name, model in overrides.items() if model},
        },
    }
    (bundle_root / "package.json").write_text(
        __import__("json").dumps(package, indent=2) + "\n", encoding="utf-8"
    )


def _manifest_reference(resource_id: str) -> str:
    return f"./{resource_relative_path(resource_id)}"


def _manifest_top_level_reference(resource_id: str) -> str:
    return f"./{PurePosixPath(resource_relative_path(resource_id)).parts[0]}"


def _manifest_skill_root_reference(resource_id: str) -> str:
    relative_path = PurePosixPath(resource_relative_path(resource_id))
    return f"./{PurePosixPath(*relative_path.parts[:-2]).as_posix()}"
