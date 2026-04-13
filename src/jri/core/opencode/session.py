import re
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..errors import JriError
from ..timeline import TimelineEvent, TimelineStore
from .client import OpenCodeProgrammatic, OpenCodeServer
from .config import (
    AGENT_FILENAMES,
    TOOL_FILENAMES,
    load_agent_text,
    load_config_text,
    load_tool_text,
)


@contextmanager
def runtime_env(*, overrides: dict[str, str | None]) -> Iterator[dict[str, str]]:
    config_text = load_config_text()
    filtered_overrides = {
        agent: model for agent, model in overrides.items() if model is not None
    }
    if filtered_overrides:
        config_text = _apply_agent_model_overrides(config_text, filtered_overrides)
    with tempfile.TemporaryDirectory(prefix="jri-opencode-") as tmp_dir:
        bundle_root = Path(tmp_dir)
        config_dir = bundle_root / ".opencode"
        agents_dir = config_dir / "agents"
        tools_dir = config_dir / "tools"
        agents_dir.mkdir(parents=True, exist_ok=True)
        tools_dir.mkdir(parents=True, exist_ok=True)
        for name in AGENT_FILENAMES:
            agents_dir.joinpath(name).write_text(
                load_agent_text(name), encoding="utf-8"
            )
        for name in TOOL_FILENAMES:
            tools_dir.joinpath(name).write_text(load_tool_text(name), encoding="utf-8")
        config_path = bundle_root / "opencode.json"
        config_path.write_text(config_text, encoding="utf-8")
        yield {
            "OPENCODE_CONFIG": str(config_path.resolve()),
            "OPENCODE_CONFIG_DIR": str(config_dir.resolve()),
        }


def call_with_server(
    opencode: OpenCodeProgrammatic,
    *,
    root: Path,
    operation: Callable[[], Any],
) -> Any:
    if isinstance(opencode, OpenCodeServer) and not opencode.is_healthy():
        with runtime_env(overrides={}) as env:
            opencode.start(env=env, cwd=root)
            try:
                return operation()
            finally:
                opencode.stop()
    return operation()


def list_sessions(
    opencode: OpenCodeProgrammatic,
    *,
    root: Path,
) -> list[dict[str, object]]:
    return call_with_server(
        opencode, root=root, operation=lambda: opencode.list_sessions(root=root)
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
    opencode: OpenCodeProgrammatic,
    *,
    root: Path,
    external_opencode_dir: Path,
    timeline: TimelineStore,
    session_id: str | None,
    task_slug: str | None = None,
) -> Path | None:
    if session_id is None:
        return None
    export_path = external_opencode_dir / f"{session_id}.json"
    export_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        call_with_server(
            opencode,
            root=root,
            operation=lambda: opencode.export_session(session_id, export_path),
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


def _apply_agent_model_overrides(config_text: str, overrides: dict[str, str]) -> str:
    updated = config_text
    for agent, model in overrides.items():
        pattern = re.compile(
            rf'("{re.escape(agent)}"\s*:\s*\{{.*?"model"\s*:\s*")([^"]+)(")',
            re.DOTALL,
        )
        updated, count = pattern.subn(rf"\g<1>{model}\g<3>", updated, count=1)
        if count != 1:
            raise JriError(f"failed to apply model override for agent '{agent}'")
    return updated
