from pathlib import Path
from typing import cast

import pytest

import jri.core.service as service_module
from jri.core.agents.client import PiRuntime
from jri.core.service import JriService
from tests.conftest import run_cli


class FakeAgentRuntimeForChat:
    def __init__(self) -> None:
        self.model: str | None = None
        self._sessions: list[dict[str, object]] = []
        self.export_calls: list[tuple[str, Path]] = []

    def list_sessions(self, *, root: Path, limit: int = 20) -> list[dict[str, object]]:
        del root, limit
        return list(self._sessions)

    def run_ralph_task(self, **kwargs):  # pragma: no cover - not used here
        del kwargs
        raise AssertionError("run_ralph_task should not be called in chat tests")

    def export_session(self, session_id: str, destination: Path) -> None:
        self.export_calls.append((session_id, destination))
        destination.write_text("{}\n", encoding="utf-8")

    def add_session(self, session_id: str, *, root: Path | None = None) -> None:
        session: dict[str, object] = {"id": session_id}
        if root is not None:
            session["directory"] = str(root.resolve())
        self._sessions.append(session)


class FakePiRuntimeForChat(PiRuntime):
    def __init__(self) -> None:
        super().__init__(binary="pi")
        self._sessions: list[dict[str, object]] = []
        self.export_calls: list[tuple[str, Path]] = []

    def list_sessions(self, *, root: Path, limit: int = 20) -> list[dict[str, object]]:
        del root, limit
        return list(self._sessions)

    def export_session(self, session_id: str, destination: Path) -> None:
        self.export_calls.append((session_id, destination))

    def add_session(self, session_id: str, *, root: Path | None = None) -> None:
        session: dict[str, object] = {"id": session_id}
        if root is not None:
            session["directory"] = str(root.resolve())
        self._sessions.append(session)


@pytest.fixture
def initialized_repo(git_repo: Path) -> Path:
    assert run_cli(["init"], cwd=git_repo) == 0
    return git_repo


def test_chat_reuses_existing_session_and_exports_it(initialized_repo: Path) -> None:
    repo = initialized_repo
    client = FakeAgentRuntimeForChat()
    launch_calls: list[dict[str, object]] = []

    def fake_launch_chat(
        *,
        root: Path,
        session_id: str | None,
        extra_args: list[str],
        binary: str = "pi",
        env: dict[str, str] | None = None,
        session_dir: Path | None = None,
    ) -> int:
        del session_dir
        launch_calls.append(
            {
                "root": root,
                "session_id": session_id,
                "extra_args": extra_args,
                "binary": binary,
                "env": env,
                "package_exists": (
                    Path(env["JRI_PI_PACKAGE"]).joinpath("package.json").is_file()
                    if env is not None
                    else False
                ),
                "interrogator_exists": (
                    Path(env["JRI_PI_PACKAGE"])
                    .joinpath("interrogator", "prompt.md")
                    .is_file()
                    if env is not None
                    else False
                ),
                "validator_exists": (
                    Path(env["JRI_PI_PACKAGE"])
                    .joinpath("interrogator", "validator", "prompt.md")
                    .is_file()
                    if env is not None
                    else False
                ),
                "explorer_exists": (
                    Path(env["JRI_PI_PACKAGE"])
                    .joinpath("explorer", "prompt.md")
                    .is_file()
                    if env is not None
                    else False
                ),
                "ralph_exists": (
                    Path(env["JRI_PI_PACKAGE"]).joinpath("ralph", "prompt.md").exists()
                    if env is not None
                    else True
                ),
                "skill_exists": (
                    Path(env["JRI_PI_PACKAGE"])
                    .joinpath("ralph", "skills", "reverse-ralph", "SKILL.md")
                    .is_file()
                    if env is not None
                    else False
                ),
                "extension_exists": (
                    Path(env["JRI_PI_PACKAGE"]).joinpath("extension.ts").is_file()
                    if env is not None
                    else False
                ),
                "tool_runner_exists": (
                    Path(env["JRI_PI_PACKAGE"])
                    .joinpath("_shared", "tools", "runner.ts")
                    .is_file()
                    if env is not None
                    else False
                ),
                "theme_exists": (
                    Path(env["JRI_PI_PACKAGE"]).joinpath("theme.json").is_file()
                    if env is not None
                    else False
                ),
            }
        )
        return 0

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service_module, "launch_chat", fake_launch_chat)
    service = JriService(repo, agent_runtime=client)
    service.state_store.save_session("existing-session-id")

    try:
        assert service.chat(["--some-pi-arg"], fresh=False) == 0
    finally:
        monkeypatch.undo()

    assert len(launch_calls) == 1
    call = launch_calls[0]
    assert call["session_id"] == "existing-session-id"
    assert call["extra_args"] == ["--some-pi-arg"]
    env = call["env"]
    assert isinstance(env, dict)
    env = cast(dict[str, str], env)
    package_root = Path(env["JRI_PI_PACKAGE"])
    assert package_root.name != ".pi"
    assert call["package_exists"] is True
    assert call["interrogator_exists"] is True
    assert call["validator_exists"] is True
    assert call["explorer_exists"] is True
    assert call["ralph_exists"] is False
    assert call["skill_exists"] is True
    assert call["extension_exists"] is True
    assert call["tool_runner_exists"] is True
    assert call["theme_exists"] is True
    assert not package_root.is_relative_to(repo)
    assert client.export_calls == [
        (
            "existing-session-id",
            service.paths.chat_logs_dir / "existing-session-id.json",
        )
    ]


def test_chat_fresh_clears_existing_session(initialized_repo: Path) -> None:
    repo = initialized_repo
    client = FakeAgentRuntimeForChat()
    session_ids: list[str | None] = []

    def fake_launch_chat(
        *,
        root: Path,
        session_id: str | None,
        extra_args: list[str],
        binary: str = "pi",
        env: dict[str, str] | None = None,
        session_dir: Path | None = None,
    ) -> int:
        del root, extra_args, binary, env, session_dir
        session_ids.append(session_id)
        return 0

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service_module, "launch_chat", fake_launch_chat)
    service = JriService(repo, agent_runtime=client)
    service.state_store.save_session("existing-session-id")

    try:
        assert service.chat([], fresh=True) == 0
    finally:
        monkeypatch.undo()

    assert session_ids == [None]
    assert service.state_store.load().session is None


def test_chat_detects_and_exports_new_session(initialized_repo: Path) -> None:
    repo = initialized_repo
    client = FakeAgentRuntimeForChat()

    def fake_launch_chat(
        *,
        root: Path,
        session_id: str | None,
        extra_args: list[str],
        binary: str = "pi",
        env: dict[str, str] | None = None,
        session_dir: Path | None = None,
    ) -> int:
        del session_id, extra_args, binary, env, session_dir
        client.add_session("new-session-id", root=root)
        return 0

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service_module, "launch_chat", fake_launch_chat)
    service = JriService(repo, agent_runtime=client)

    try:
        assert service.chat([], fresh=False) == 0
    finally:
        monkeypatch.undo()

    assert service.state_store.load().session == "new-session-id"
    assert client.export_calls == [
        ("new-session-id", service.paths.chat_logs_dir / "new-session-id.json")
    ]


def test_chat_detects_new_pi_session_after_chat(
    initialized_repo: Path,
) -> None:
    repo = initialized_repo
    client = FakePiRuntimeForChat()

    def fake_launch_chat(
        *,
        root: Path,
        session_id: str | None,
        extra_args: list[str],
        binary: str = "pi",
        env: dict[str, str] | None = None,
        session_dir: Path | None = None,
    ) -> int:
        del session_id, extra_args, binary, env, session_dir
        client.add_session("temporary-rpc-session", root=root)
        return 0

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service_module, "launch_chat", fake_launch_chat)
    service = JriService(repo, agent_runtime=client)

    try:
        assert service.chat([], fresh=False) == 0
    finally:
        monkeypatch.undo()

    assert service.state_store.load().session == "temporary-rpc-session"
    assert client.export_calls == []


def test_chat_starts_fresh_when_saved_pi_session_is_missing(
    initialized_repo: Path,
) -> None:
    repo = initialized_repo
    client = FakePiRuntimeForChat()
    session_ids: list[str | None] = []

    def fake_launch_chat(
        *,
        root: Path,
        session_id: str | None,
        extra_args: list[str],
        binary: str = "pi",
        env: dict[str, str] | None = None,
        session_dir: Path | None = None,
    ) -> int:
        del root, extra_args, binary, env, session_dir
        session_ids.append(session_id)
        return 0

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service_module, "launch_chat", fake_launch_chat)
    service = JriService(repo, agent_runtime=client)
    service.state_store.save_session("stale-session")

    try:
        assert service.chat([], fresh=False) == 0
    finally:
        monkeypatch.undo()

    assert session_ids == [None]
    assert service.state_store.load().session is None
    assert client.export_calls == []


def test_chat_resumes_saved_pi_session_when_it_exists(
    initialized_repo: Path,
) -> None:
    repo = initialized_repo
    client = FakePiRuntimeForChat()
    client.add_session("existing-pi-session", root=repo)
    session_ids: list[str | None] = []

    def fake_launch_chat(
        *,
        root: Path,
        session_id: str | None,
        extra_args: list[str],
        binary: str = "pi",
        env: dict[str, str] | None = None,
        session_dir: Path | None = None,
    ) -> int:
        del root, extra_args, binary, env, session_dir
        session_ids.append(session_id)
        return 0

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service_module, "launch_chat", fake_launch_chat)
    service = JriService(repo, agent_runtime=client)
    service.state_store.save_session("existing-pi-session")

    try:
        assert service.chat([], fresh=False) == 0
    finally:
        monkeypatch.undo()

    assert session_ids == ["existing-pi-session"]
    assert service.state_store.load().session == "existing-pi-session"
    assert client.export_calls == []


def test_chat_saves_newer_pi_session_when_chat_creates_one(
    initialized_repo: Path,
) -> None:
    repo = initialized_repo
    client = FakePiRuntimeForChat()
    client.add_session("existing-pi-session", root=repo)
    session_ids: list[str | None] = []

    def fake_launch_chat(
        *,
        root: Path,
        session_id: str | None,
        extra_args: list[str],
        binary: str = "pi",
        env: dict[str, str] | None = None,
        session_dir: Path | None = None,
    ) -> int:
        del extra_args, binary, env, session_dir
        session_ids.append(session_id)
        client.add_session("newer-pi-session", root=root)
        return 0

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service_module, "launch_chat", fake_launch_chat)
    service = JriService(repo, agent_runtime=client)
    service.state_store.save_session("existing-pi-session")

    try:
        assert service.chat([], fresh=False) == 0
    finally:
        monkeypatch.undo()

    assert session_ids == ["existing-pi-session"]
    assert service.state_store.load().session == "newer-pi-session"
    assert client.export_calls == []
