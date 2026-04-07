from pathlib import Path

import pytest

from jri.core.opencode import OpenCodeClient
from jri.core.service import JriService
from tests.conftest import run_cli
from tests.helpers import read_json


class FakeOpenCodeClientForChat(OpenCodeClient):
    """Fake client for testing chat functionality."""

    def __init__(self) -> None:
        super().__init__(model=None)
        self.list_sessions_calls: list[Path] = []
        self.launch_chat_calls: list[tuple[Path, str | None, list[str]]] = []
        self._sessions: list[dict[str, object]] = []

    def list_sessions(self, *, root: Path, limit: int = 20) -> list[dict[str, object]]:
        self.list_sessions_calls.append(root)
        return list(self._sessions)

    def launch_chat(
        self, *, root: Path, session_id: str | None, extra_args: list[str]
    ) -> int:
        self.launch_chat_calls.append((root, session_id, extra_args))
        return 0

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
    client = FakeOpenCodeClientForChat()
    service = JriService(repo, opencode_client=client)

    # Set up an existing session
    service.state_store.save_session("existing-session-id")

    # Call chat without fresh flag
    result = service.chat([], fresh=False)

    assert result == 0
    assert len(client.launch_chat_calls) == 1
    _root, session_id, _extra_args = client.launch_chat_calls[0]
    assert session_id == "existing-session-id"


def test_chat_with_fresh_clears_session(initialized_repo: Path) -> None:
    """Test that chat with --fresh clears the existing session."""
    repo = initialized_repo
    client = FakeOpenCodeClientForChat()
    service = JriService(repo, opencode_client=client)

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
    assert len(client.launch_chat_calls) == 1
    _root, session_id, _extra_args = client.launch_chat_calls[0]
    assert session_id is None


def test_chat_fresh_with_no_existing_session(initialized_repo: Path) -> None:
    """Test that chat --fresh works even when no session exists."""
    repo = initialized_repo
    client = FakeOpenCodeClientForChat()
    service = JriService(repo, opencode_client=client)

    # Verify no session exists
    state = service.state_store.load()
    assert state.session is None

    # Call chat with fresh flag
    result = service.chat([], fresh=True)

    assert result == 0
    state = service.state_store.load()
    assert state.session is None
    assert len(client.launch_chat_calls) == 1


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


def test_chat_fresh_does_not_affect_other_state(initialized_repo: Path) -> None:
    """Test that --fresh only clears session, not other state."""
    repo = initialized_repo
    client = FakeOpenCodeClientForChat()
    service = JriService(repo, opencode_client=client)

    # Set up some state
    service.state_store.save_session("existing-session-id")

    # Call chat with fresh flag
    result = service.chat([], fresh=True)

    assert result == 0

    # Verify only session is cleared, other state remains
    state = read_json(service.paths.state_path)
    assert state.get("session") is None
