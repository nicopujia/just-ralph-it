"""TTY functional tests for the user-facing JRI CLI contract."""

from dataclasses import replace
from pathlib import Path

from tests.env import INTERVIEWER_FACTORY_ENV
from tests.support.cli_result import CliRun
from tests.support.cli_tty import CliTtyHarness

SHIFT_ENTER = "\x1b[13;2u"
SHIFT_UP = "\x1b[1;2A"
SHIFT_DOWN = "\x1b[1;2B"
SHIFT_RIGHT = "\x1b[1;2C"
SHIFT_LEFT = "\x1b[1;2D"


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


def test_tty_text_delta_first_token_is_printed_and_logged_once(
    tmp_path: Path,
    cli_tty: CliTtyHarness,
) -> None:
    """TTY streaming preserves a first-token contraction exactly once."""
    harness = _with_interviewer_factory(
        cli_tty,
        "tests.doubles.interviewers:create_first_token_interviewer",
    )
    session = harness.spawn(cwd=tmp_path)

    session.expect_prompt()
    session.sendline("Start with deltas.")
    session.expect_prompt()
    session.send_eof()
    result = session.expect_eof()

    logged = "\n".join(result.assistant_messages())
    assert result.returncode == 0
    assert result.stdout.count("I'm") == 1
    assert logged.count("I'm") == 1
    assert "I'm checking the first token." in result.stdout
    assert "I'm checking the first token." in logged


def test_shift_enter_inserts_repeated_newlines_before_tty_submit(
    tmp_path: Path,
    cli_tty: CliTtyHarness,
) -> None:
    """Shift+Enter inserts newlines until plain Enter submits the turn."""
    session = cli_tty.spawn(cwd=tmp_path)

    session.expect_prompt()
    session.send("first")
    session.send(SHIFT_ENTER)
    session.send("second")
    session.send(SHIFT_ENTER)
    session.send("third")
    session.send_enter()
    session.expect_prompt()
    session.send_eof()
    result = session.expect_eof()

    assert result.returncode == 0
    assert result.user_messages()[0] == "first\nsecond\nthird"


def test_modified_tty_horizontal_arrow_escapes_move_cursor_before_submit(
    tmp_path: Path,
    cli_tty: CliTtyHarness,
) -> None:
    """Modified horizontal arrows move the input cursor."""
    session = cli_tty.spawn(cwd=tmp_path)

    session.expect_prompt()
    session.send("ac")
    session.send(SHIFT_LEFT)
    session.send("b")
    session.send(SHIFT_RIGHT)
    session.send("d")
    session.send_enter()
    session.expect_prompt()
    session.send_eof()
    result = session.expect_eof()

    assert result.returncode == 0
    assert result.user_messages()[0] == "abcd"


def test_modified_tty_vertical_arrow_escapes_move_cursor_before_submit(
    tmp_path: Path,
    cli_tty: CliTtyHarness,
) -> None:
    """Modified vertical arrows move within multi-line input."""
    session = cli_tty.spawn(cwd=tmp_path)

    session.expect_prompt()
    session.send("top")
    session.send(SHIFT_ENTER)
    session.send("bottom")
    session.send(SHIFT_UP)
    session.send("!")
    session.send(SHIFT_DOWN)
    session.send("?")
    session.send_enter()
    session.expect_prompt()
    session.send_eof()
    result = session.expect_eof()

    assert result.returncode == 0
    assert result.user_messages()[0] == "top!\nbott?om"


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
    _assert_failure_debug_logs_archived(result)


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
    assert "Finalizing specs..." in result.stdout
    assert "finalize_specs" in result.output
    assert "just_ralph_it" not in result.output
    assert "Ralph is coming soon to JRI" in result.stdout
    assert "handoff" not in result.output.lower()
    assert "built" not in result.output.lower()
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


def _assert_failure_debug_logs_archived(result: CliRun) -> None:
    _assert_debug_logs_archived(result)
    assert result.returncode != 0
    assert result.debug_log_dir is not None
    assert (result.debug_log_dir / "returncode.txt").read_text(
        encoding="utf-8",
    ) == f"{result.returncode}\n"
    assert (
        (result.debug_log_dir / "stdout.txt")
        .read_bytes()
        .decode(encoding="utf-8")
    ) == result.stdout
    assert (
        (result.debug_log_dir / "stderr.txt")
        .read_bytes()
        .decode(encoding="utf-8")
    ) == result.stderr


def _with_interviewer_factory(
    harness: CliTtyHarness,
    factory: str,
) -> CliTtyHarness:
    env = dict(harness.env)
    env[INTERVIEWER_FACTORY_ENV] = factory
    return replace(harness, env=env)
