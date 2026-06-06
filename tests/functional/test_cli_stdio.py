"""Stdio functional tests for the user-facing JRI CLI contract.

These tests invoke the repo-local `jri` console script in subprocesses with
deterministic interviewer doubles, including during full validation.
"""

import os
from dataclasses import replace
from pathlib import Path

from tests.doubles.interviewers import HIDDEN_SPEC_PHRASE
from tests.env import INTERVIEWER_FACTORY_ENV
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


def test_nested_run_loads_project_root_dotenv_before_validation(
    tmp_path: Path,
    cli_stdio: CliStdioHarness,
) -> None:
    """A child cwd can rely on the project-root .env for CLI config."""
    project = tmp_path / "project"
    child = project / "app" / "api"
    child.mkdir(parents=True)
    cli_stdio.initialize_git_repo(project)
    (project / ".env").write_text(
        (
            "JRI_INTERVIEWER_FACTORY="
            "tests.doubles.interviewers:create_scripted_interviewer\n"
        ),
        encoding="utf-8",
    )
    env = dict(cli_stdio.env)
    env.pop(INTERVIEWER_FACTORY_ENV, None)
    env.pop("OPENROUTER_API_KEY", None)
    env["PYTHONPATH"] = _prepend_pythonpath(
        Path(__file__).resolve().parents[2],
        env.get("PYTHONPATH"),
    )
    harness = replace(cli_stdio, env=env)

    result = harness.run(cwd=child)

    assert result.returncode == 0
    assert not result.stderr
    assert (project / ".jri" / "logs" / "interview.jsonl").exists()
    assert not (child / ".jri").exists()


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


def test_text_delta_first_token_is_printed_and_logged_once(
    tmp_path: Path,
    cli_stdio: CliStdioHarness,
) -> None:
    """Streaming output preserves a first-token contraction exactly once."""
    harness = _with_interviewer_factory(
        cli_stdio,
        "tests.doubles.interviewers:create_first_token_interviewer",
    )

    result = harness.run(cwd=tmp_path, input_text="Start with deltas.\n")

    logged = "\n".join(result.assistant_messages())
    assert result.returncode == 0
    assert not result.stderr
    assert result.stdout.count("I'm") == 1
    assert logged.count("I'm") == 1
    assert "I'm checking the first token." in result.stdout
    assert "I'm checking the first token." in logged


def test_exact_specs_are_hidden_until_user_asks(
    tmp_path: Path,
    cli_stdio: CliStdioHarness,
) -> None:
    """Persisted exact spec text is not printed unless directly requested."""
    harness = _with_interviewer_factory(
        cli_stdio,
        "tests.doubles.interviewers:create_hidden_spec_interviewer",
    )

    first_result = harness.run(
        cwd=tmp_path,
        input_text="Capture this product idea.\n",
    )
    second_result = harness.run(
        cwd=tmp_path,
        input_text="Show me the exact specs.\n",
    )

    spec_text = (tmp_path / ".jri" / "specs" / "product.md").read_text(
        encoding="utf-8",
    )
    assert first_result.returncode == 0
    assert not first_result.stderr
    assert HIDDEN_SPEC_PHRASE in spec_text
    assert HIDDEN_SPEC_PHRASE not in first_result.stdout
    assert second_result.returncode == 0
    assert not second_result.stderr
    assert HIDDEN_SPEC_PHRASE in second_result.stdout


def test_non_software_input_stays_conversational_without_specs(
    tmp_path: Path,
    cli_stdio: CliStdioHarness,
) -> None:
    """Non-software input gets a conversation, not specs or finalization."""
    harness = _with_interviewer_factory(
        cli_stdio,
        "tests.doubles.interviewers:create_non_software_interviewer",
    )

    result = harness.run(
        cwd=tmp_path,
        input_text="I am deciding what to cook tonight.\n",
    )

    spec_files = list((tmp_path / ".jri" / "specs").glob("**/*.md"))
    assert result.returncode == 0
    assert not result.stderr
    assert result.finish_reason() == "eof"
    assert "software project" in result.stdout
    assert "Finalizing specs..." not in result.stdout
    assert "# Product" not in result.stdout
    assert not spec_files
    assert not result.has_commit()


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
    assert "I can't finalize specs yet." in result.stdout
    assert "Ralph handoff" not in result.output
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


def _with_interviewer_factory(
    harness: CliStdioHarness,
    factory: str,
) -> CliStdioHarness:
    env = dict(harness.env)
    env[INTERVIEWER_FACTORY_ENV] = factory
    return replace(harness, env=env)


def _prepend_pythonpath(path: Path, existing: str | None) -> str:
    if not existing:
        return str(path)
    return f"{path}{os.pathsep}{existing}"
