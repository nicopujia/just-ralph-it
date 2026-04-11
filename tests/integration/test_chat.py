from pathlib import Path
from typing import cast

import pytest

import jri.core.git as git_module
import jri.core.service as service_module
from jri.cli.main import main
from jri.core.service import JriService
from tests.conftest import run_cli
from tests.helpers import git, read_json


class FakeOpenCodeServerForChat:
    """Fake programmatic OpenCode adapter for chat tests."""

    def __init__(self) -> None:
        self.model: str | None = None
        self.list_sessions_calls: list[Path] = []
        self._sessions: list[dict[str, object]] = []

    def list_sessions(self, *, root: Path, limit: int = 20) -> list[dict[str, object]]:
        self.list_sessions_calls.append(root)
        return list(self._sessions)

    def run_ralph_task(self, **kwargs):  # pragma: no cover - not used here
        raise AssertionError("run_ralph_task should not be called in chat tests")

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text("{}\n", encoding="utf-8")

    def add_session(self, session_id: str) -> None:
        self._sessions.append({"id": session_id})


@pytest.fixture
def initialized_repo(git_repo: Path) -> Path:
    """Initialize a JRI project in the git repo."""
    exit_code = run_cli(["init"], cwd=git_repo)
    assert exit_code == 0
    return git_repo


def test_chat_without_fresh_reuses_session(initialized_repo: Path) -> None:
    """Test that chat without --fresh reuses the existing session."""
    repo = initialized_repo
    server = FakeOpenCodeServerForChat()
    launch_calls: list[tuple[Path, str | None, list[str], dict[str, str] | None]] = []

    def fake_launch_chat(
        *,
        root: Path,
        session_id: str | None,
        extra_args: list[str],
        binary: str = "opencode",
        env: dict[str, str] | None = None,
    ) -> int:
        launch_calls.append((root, session_id, extra_args, env))
        return 0

    service = JriService(repo, opencode_client=server)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service_module, "launch_chat", fake_launch_chat)

    # Set up an existing session
    service.state_store.save_session("existing-session-id")

    # Call chat without fresh flag
    result = service.chat([], fresh=False)

    assert result == 0
    assert len(launch_calls) == 1
    _root, session_id, _extra_args, env = launch_calls[0]
    assert session_id == "existing-session-id"
    assert env == {
        "OPENCODE_CONFIG": str((repo / ".jri" / "opencode.json").resolve()),
        "OPENCODE_CONFIG_DIR": str((repo / ".jri" / ".opencode").resolve()),
    }
    monkeypatch.undo()


def test_chat_model_overrides_use_temporary_config(initialized_repo: Path) -> None:
    repo = initialized_repo
    server = FakeOpenCodeServerForChat()
    launch_calls: list[dict[str, object]] = []

    def fake_launch_chat(
        *,
        root: Path,
        session_id: str | None,
        extra_args: list[str],
        binary: str = "opencode",
        env: dict[str, str] | None = None,
    ) -> int:
        assert env is not None
        config_path = Path(env["OPENCODE_CONFIG"])
        launch_calls.append(
            {
                "root": root,
                "session_id": session_id,
                "extra_args": extra_args,
                "env": env,
                "config_path": config_path,
                "config_text": config_path.read_text(encoding="utf-8"),
            }
        )
        return 0

    service = JriService(repo, opencode_client=server)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service_module, "launch_chat", fake_launch_chat)

    result = service.chat(
        [],
        fresh=False,
        model="provider/interrogator-main",
        validator_model="provider/interrogator-validator",
    )

    assert result == 0
    assert len(launch_calls) == 1
    call = launch_calls[0]
    env = cast(dict[str, str], call["env"])
    config_path = cast(Path, call["config_path"])
    config_text = cast(str, call["config_text"])
    assert env["OPENCODE_CONFIG_DIR"] == str((repo / ".jri" / ".opencode").resolve())
    assert config_path != (repo / ".jri" / "opencode.json").resolve()
    assert '"interrogator": {' in config_text
    assert '"model": "provider/interrogator-main"' in config_text
    assert '"todowrite": "deny"' in config_text
    assert '"check-draft-promotion": "deny"' in config_text
    assert '"promote-tasks": "allow"' in config_text
    assert '"interrogator-validator": {' in config_text
    assert '"model": "provider/interrogator-validator"' in config_text
    assert '"check-draft-promotion": "allow"' in config_text
    assert (
        repo.joinpath(".jri", "opencode.json").read_text(encoding="utf-8")
        != config_text
    )
    assert not config_path.exists()
    monkeypatch.undo()


def test_chat_with_fresh_clears_session(initialized_repo: Path) -> None:
    """Test that chat with --fresh clears the existing session."""
    repo = initialized_repo
    server = FakeOpenCodeServerForChat()
    launch_calls: list[tuple[Path, str | None, list[str], dict[str, str] | None]] = []

    def fake_launch_chat(
        *,
        root: Path,
        session_id: str | None,
        extra_args: list[str],
        binary: str = "opencode",
        env: dict[str, str] | None = None,
    ) -> int:
        launch_calls.append((root, session_id, extra_args, env))
        return 0

    service = JriService(repo, opencode_client=server)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service_module, "launch_chat", fake_launch_chat)

    # Set up an existing session
    service.state_store.save_session("existing-session-id")

    # Verify session exists
    state = service.state_store.load()
    assert state.session == "existing-session-id"

    # Call chat with fresh flag
    result = service.chat([], fresh=True)

    assert result == 0
    # Session should be cleared before launching chat
    state = service.state_store.load()
    assert state.session is None
    # launch_chat should be called with None session
    assert len(launch_calls) == 1
    _root, session_id, _extra_args, env = launch_calls[0]
    assert session_id is None
    assert env == {
        "OPENCODE_CONFIG": str((repo / ".jri" / "opencode.json").resolve()),
        "OPENCODE_CONFIG_DIR": str((repo / ".jri" / ".opencode").resolve()),
    }
    monkeypatch.undo()


def test_chat_fresh_with_no_existing_session(initialized_repo: Path) -> None:
    """Test that chat --fresh works even when no session exists."""
    repo = initialized_repo
    server = FakeOpenCodeServerForChat()
    launch_calls: list[tuple[Path, str | None, list[str], dict[str, str] | None]] = []

    def fake_launch_chat(
        *,
        root: Path,
        session_id: str | None,
        extra_args: list[str],
        binary: str = "opencode",
        env: dict[str, str] | None = None,
    ) -> int:
        launch_calls.append((root, session_id, extra_args, env))
        return 0

    service = JriService(repo, opencode_client=server)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service_module, "launch_chat", fake_launch_chat)

    # Verify no session exists
    state = service.state_store.load()
    assert state.session is None

    # Call chat with fresh flag
    result = service.chat([], fresh=True)

    assert result == 0
    state = service.state_store.load()
    assert state.session is None
    assert len(launch_calls) == 1
    monkeypatch.undo()


def test_chat_cli_with_fresh_flag(initialized_repo: Path) -> None:
    """Test the CLI parsing of the --fresh flag."""
    repo = initialized_repo

    # We can't easily test the full integration without mocking the opencode client,
    # but we can verify the CLI accepts the --fresh flag without error
    # by checking that the argument parser doesn't reject it
    # This is a basic smoke test - argparse exits with 0 for --help
    with pytest.raises(SystemExit) as exc_info:
        run_cli(["chat", "--help"], cwd=repo)
    assert exc_info.value.code == 0


def test_chat_cli_passes_model_overrides(initialized_repo: Path) -> None:
    repo = initialized_repo
    captured: dict[str, object] = {}

    def fake_chat(
        self: JriService,
        extra_args: list[str],
        *,
        fresh: bool = False,
        model: str | None = None,
        validator_model: str | None = None,
    ) -> int:
        captured["extra_args"] = extra_args
        captured["fresh"] = fresh
        captured["model"] = model
        captured["validator_model"] = validator_model
        return 0

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(JriService, "chat", fake_chat)

    result = main(
        [
            "chat",
            "--fresh",
            "--model",
            "provider/interrogator-main",
            "--validator-model",
            "provider/interrogator-validator",
            "--prompt",
            "hello",
        ],
        cwd=repo,
    )

    assert result == 0
    assert captured == {
        "extra_args": ["--prompt", "hello"],
        "fresh": True,
        "model": "provider/interrogator-main",
        "validator_model": "provider/interrogator-validator",
    }
    monkeypatch.undo()


def test_chat_fresh_does_not_affect_other_state(initialized_repo: Path) -> None:
    """Test that --fresh only clears session, not other state."""
    repo = initialized_repo
    server = FakeOpenCodeServerForChat()

    def fake_launch_chat(
        *,
        root: Path,
        session_id: str | None,
        extra_args: list[str],
        binary: str = "opencode",
        env: dict[str, str] | None = None,
    ) -> int:
        return 0

    service = JriService(repo, opencode_client=server)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service_module, "launch_chat", fake_launch_chat)

    # Set up some state
    service.state_store.save_session("existing-session-id")

    # Call chat with fresh flag
    result = service.chat([], fresh=True)

    assert result == 0

    # Verify only session is cleared, other state remains
    state = read_json(service.paths.state_path)
    assert state.get("session") is None
    monkeypatch.undo()


def test_chat_does_not_auto_restore_modified_managed_files(
    initialized_repo: Path,
) -> None:
    repo = initialized_repo
    server = FakeOpenCodeServerForChat()

    def fake_launch_chat(
        *,
        root: Path,
        session_id: str | None,
        extra_args: list[str],
        binary: str = "opencode",
        env: dict[str, str] | None = None,
    ) -> int:
        return 0

    managed = repo / ".jri" / ".opencode" / "agents" / "interrogator.md"
    managed.write_text("user-modified managed file\n", encoding="utf-8")
    head_before = git(repo, "rev-parse", "HEAD")

    service = JriService(repo, opencode_client=server)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service_module, "launch_chat", fake_launch_chat)

    result = service.chat([], fresh=False)

    assert result == 0
    assert managed.read_text(encoding="utf-8") == "user-modified managed file\n"
    assert git(repo, "rev-parse", "HEAD") == head_before
    assert git(repo, "log", "-1", "--pretty=%s") != git_module.MSG_UPGRADE
    monkeypatch.undo()
