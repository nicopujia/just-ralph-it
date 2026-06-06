"""Stdio functional tests for the user-facing JRI CLI contract.

These tests invoke the repo-local `jri` console script in subprocesses. With
`--live`, the same scenarios run without interviewer doubles and are the live
CLI contract lane.
"""

from pathlib import Path

from tests.support.cli_result import CliRun
from tests.support.cli_stdio import CliStdioHarness


def test_help_does_not_require_model_credentials(
    credentialless_cli_stdio: CliStdioHarness,
) -> None:
    """The CLI help works without model credentials."""
    result = credentialless_cli_stdio.run_help()

    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert not result.stderr


def test_invalid_option_does_not_initialize_project(
    tmp_path: Path,
    credentialless_cli_stdio: CliStdioHarness,
) -> None:
    """Invalid CLI options fail before project mutation."""
    result = credentialless_cli_stdio.run(
        cwd=tmp_path,
        args=("--not-a-real-flag",),
    )

    assert result.returncode != 0
    assert "usage:" in result.stderr
    assert not result.jri_dir.exists()


def test_interactive_run_requires_selected_provider_credentials(
    tmp_path: Path,
    credentialless_cli_stdio: CliStdioHarness,
) -> None:
    """Interactive sessions fail fast without selected-provider credentials."""
    result = credentialless_cli_stdio.run(cwd=tmp_path)

    assert result.returncode != 0
    assert "OPENROUTER_API_KEY is required" in result.stderr
    assert not result.jri_dir.exists()


def test_interactive_run_loads_pwd_dotenv_credentials(
    tmp_path: Path,
    credentialless_cli_stdio: CliStdioHarness,
) -> None:
    """Interactive sessions read provider credentials from cwd .env."""
    (tmp_path / ".env").write_text(
        (
            "OPENROUTER_API_KEY=fake\n"
            "JRI_INTERVIEWER_MODEL_ID=test\n"
            "JRI_EXPLORER_MODEL_ID=test\n"
        ),
        encoding="utf-8",
    )

    result = credentialless_cli_stdio.run(cwd=tmp_path)

    assert result.returncode == 0
    assert not result.stderr
    assert result.jri_dir.exists()


def test_empty_directory_initializes_git_and_jri_state(
    tmp_path: Path,
    cli_stdio: CliStdioHarness,
) -> None:
    """A CLI run initializes a new project."""
    result = cli_stdio.run(cwd=tmp_path)

    assert result.returncode == 0
    assert (tmp_path / ".git").is_dir()
    assert (tmp_path / ".jri" / ".gitignore").read_text() == "logs/\n"
    assert (tmp_path / ".jri" / "scratchpad.md").exists()
    assert (tmp_path / ".jri" / "specs").is_dir()
    assert (tmp_path / ".jri" / "logs" / "interview.jsonl").exists()


def test_force_recreates_existing_jri_state(
    tmp_path: Path,
    cli_stdio: CliStdioHarness,
) -> None:
    """The --force option recreates the active .jri directory."""
    jri_dir = tmp_path / ".jri"
    jri_dir.mkdir()
    (jri_dir / "scratchpad.md").write_text("stale\n", encoding="utf-8")

    result = cli_stdio.run(cwd=tmp_path, args=("--force",))

    assert result.returncode == 0
    assert not result.stderr
    assert (jri_dir / "scratchpad.md").read_text(encoding="utf-8") != "stale\n"
    assert (jri_dir / "specs").is_dir()
    assert (jri_dir / "logs" / "interview.jsonl").exists()


def test_child_directory_uses_existing_parent_jri(
    tmp_path: Path,
    cli_stdio: CliStdioHarness,
) -> None:
    """Running in a child directory uses the parent project session."""
    project = tmp_path / "project"
    child = project / "app" / "api"
    (project / ".jri").mkdir(parents=True)
    child.mkdir(parents=True)

    result = cli_stdio.run(cwd=child)

    assert result.returncode == 0
    assert (project / ".jri" / "scratchpad.md").exists()
    assert not (child / ".jri").exists()


def test_first_interview_turn_records_visible_response(
    tmp_path: Path,
    cli_stdio: CliStdioHarness,
    first_turn_input: str,
) -> None:
    """A first interview turn produces visible output and logs cleanly."""
    result = cli_stdio.run(cwd=tmp_path, input_text=first_turn_input)

    assert result.returncode == 0
    assert not result.stderr
    assert "jri>" in result.stdout
    assert result.has_visible_assistant_output()
    assert not result.has_commit()
    _assert_debug_logs_archived(result)
    _assert_successful_interview_log(result, assistant_messages=1)


def test_early_just_ralph_it_keeps_interview_open_without_commit(
    tmp_path: Path,
    cli_stdio: CliStdioHarness,
    early_trigger_input: str,
) -> None:
    """Early finalization is rejected until required behavior is known."""
    cli_stdio.initialize_git_repo(tmp_path)

    result = cli_stdio.run(cwd=tmp_path, input_text=early_trigger_input)

    assert result.returncode == 0
    assert not result.stderr
    assert result.has_visible_assistant_output()
    assert result.has_assistant_response_after_last_user_message()
    assert len(result.user_messages()) >= 2
    assert result.finish_reason() == "eof"
    assert not result.has_commit()
    _assert_successful_interview_log(result, assistant_messages=1)


def test_mvp_happy_path_finalizes_and_commits_jri_files(
    tmp_path: Path,
    cli_stdio: CliStdioHarness,
    mvp_happy_path_input: str,
) -> None:
    """A ready interview finalizes and commits only JRI-owned files."""
    cli_stdio.initialize_git_repo(tmp_path)

    result = cli_stdio.run(cwd=tmp_path, input_text=mvp_happy_path_input)

    committed = result.committed_files()
    spec_text = result.committed_spec_text().lower()
    assert result.returncode == 0
    assert not result.stderr
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
