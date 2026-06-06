"""TTY functional tests for the user-facing JRI CLI contract."""

from pathlib import Path

from tests.support.cli_result import CliRun
from tests.support.cli_tty import CliTtyHarness


def test_initial_prompt_appears_in_real_tty(
    tmp_path: Path,
    cli_tty: CliTtyHarness,
) -> None:
    """The CLI renders an initial prompt in a real TTY session."""
    session = cli_tty.spawn(cwd=tmp_path)

    session.expect_prompt()
    session.send_eof()
    result = session.expect_eof()

    assert result.returncode == 0
    assert "jri>" in result.output
    assert result.finish_reason() == "eof"


def test_first_tty_turn_records_visible_response(
    tmp_path: Path,
    cli_tty: CliTtyHarness,
    first_turn_input: str,
) -> None:
    """A TTY interview turn produces visible output and logs cleanly."""
    session = cli_tty.spawn(cwd=tmp_path)

    session.expect_prompt()
    session.sendline(_single_line(first_turn_input))
    session.expect_prompt()
    session.send_eof()
    result = session.expect_eof()

    assert result.returncode == 0
    assert result.has_visible_assistant_output()
    assert not result.has_commit()
    _assert_debug_logs_archived(result)
    _assert_successful_interview_log(result, assistant_messages=1)


def test_ctrl_d_exits_cleanly(
    tmp_path: Path,
    cli_tty: CliTtyHarness,
) -> None:
    """Ctrl-D exits the TTY session cleanly."""
    session = cli_tty.spawn(cwd=tmp_path)

    session.expect_prompt()
    session.send_eof()
    result = session.expect_eof()

    assert result.returncode == 0
    assert result.finish_reason() == "eof"


def test_ctrl_c_exits_with_interrupt_status(
    tmp_path: Path,
    cli_tty: CliTtyHarness,
) -> None:
    """Ctrl-C cancels the TTY session with shell interrupt status."""
    session = cli_tty.spawn(cwd=tmp_path)

    session.expect_prompt()
    session.send_interrupt()
    result = session.expect_eof()

    assert result.returncode == 130
    assert "Cancelled." in result.output
    assert result.finish_reason() == "keyboard_interrupt"


def test_tty_mvp_happy_path_finalizes_and_commits_jri_files(
    tmp_path: Path,
    cli_tty: CliTtyHarness,
    mvp_happy_path_input: str,
) -> None:
    """A ready TTY interview finalizes and commits only JRI-owned files."""
    cli_tty.initialize_git_repo(tmp_path)
    lines = mvp_happy_path_input.splitlines()
    session = cli_tty.spawn(cwd=tmp_path)

    session.expect_prompt()
    session.sendline(lines[0])
    session.expect_prompt()
    session.sendline(lines[1])
    session.expect_prompt()
    session.sendline(lines[2])
    result = session.expect_eof()

    committed = result.committed_files()
    spec_text = result.committed_spec_text().lower()
    assert result.returncode == 0
    assert result.has_visible_assistant_output()
    assert result.finish_reason() == "just_ralph_it"
    assert result.has_commit()
    assert ".jri/.gitignore" in committed
    assert ".jri/scratchpad.md" in committed
    assert any(
        path.startswith(".jri/specs/") and path.endswith(".md")
        for path in committed
    )
    assert all(path.startswith(".jri/") for path in committed)
    assert ".jri/logs/interview.jsonl" not in committed
    assert "hello" in spec_text
    assert "cli" in spec_text or "command" in spec_text
    assert "stdout" in spec_text or "standard output" in spec_text
    _assert_successful_interview_log(result, assistant_messages=1)


def _single_line(value: str) -> str:
    lines = value.splitlines()
    assert len(lines) == 1
    return lines[0]


def _assert_successful_interview_log(
    result: CliRun,
    *,
    assistant_messages: int,
) -> None:
    types = result.event_types()
    messages = result.assistant_messages()

    assert "session_started" in types
    assert "user_message" in types
    assert "error" not in types
    assert len(messages) >= assistant_messages
    assert all(message.strip() for message in messages)


def _assert_debug_logs_archived(result: CliRun) -> None:
    assert result.debug_log_dir is not None
    assert result.debug_log_dir.is_relative_to(Path.cwd() / ".pytest_logs")
    assert (result.debug_log_dir / "logs" / "interview.jsonl").exists()
    assert (result.debug_log_dir / "stdout.txt").exists()
    assert (result.debug_log_dir / "stderr.txt").exists()
    assert ".pytest_logs/" in Path(".gitignore").read_text(encoding="utf-8")
