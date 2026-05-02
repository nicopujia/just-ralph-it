import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..errors import JriError
from ..timeline import TimelineEvent, TimelineStore
from .client import AgentRuntime, PiRuntime
from .config import (
    COPYABLE_DIRECTORIES,
    iter_directory_assets,
    load_asset_text,
)


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
        for directory in COPYABLE_DIRECTORIES:
            target_dir = bundle_root / directory
            target_dir.mkdir(parents=True, exist_ok=True)
            for name in iter_directory_assets(directory):
                if (
                    directory == "prompts"
                    and included_agents is not None
                    and Path(name).stem not in included_agents
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


def _write_package_manifest(
    bundle_root: Path, *, overrides: dict[str, str | None]
) -> None:
    package = {
        "name": "jri-pi-runtime",
        "private": True,
        "keywords": ["pi-package"],
        "pi": {
            "extensions": ["./extensions/jri.ts"],
            "skills": ["./skills"],
            "prompts": ["./prompts"],
            "tools": ["./tools"],
        },
        "jri": {
            "models": {name: model for name, model in overrides.items() if model},
        },
    }
    (bundle_root / "package.json").write_text(
        __import__("json").dumps(package, indent=2) + "\n", encoding="utf-8"
    )
