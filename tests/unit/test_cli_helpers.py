import importlib
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

cli_main = importlib.import_module("jri.cli.main")


def test_finalize_command_return_reports_nonzero_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main._finalize_command_return("start", 7) == 7

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "start: command exited with status 7\n"


def test_finalize_command_return_leaves_success_quiet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main._finalize_command_return("chat", 0) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_override_remaining_tasks_uses_valid_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JRI_REMAINING_TASKS", "3")

    assert cli_main._override_remaining_tasks(10) == 3


def test_override_remaining_tasks_keeps_argument_when_env_missing_or_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JRI_REMAINING_TASKS", raising=False)
    assert cli_main._override_remaining_tasks(5) == 5

    monkeypatch.setenv("JRI_REMAINING_TASKS", "many")
    assert cli_main._override_remaining_tasks(5) == 5


def test_format_timeline_detail_adds_known_reason_summary() -> None:
    detail: dict[str, object] = {"reason": "task_failed", "task": "build-api"}

    assert cli_main._format_timeline_detail(detail) == (
        "reason=task_failed task=build-api summary=Task-returned-to-todo"
    )
    assert detail == {"reason": "task_failed", "task": "build-api"}


def test_format_timeline_detail_preserves_existing_summary() -> None:
    detail: dict[str, object] = {
        "reason": "needs_human",
        "summary": "Custom",
        "count": 2,
    }

    assert cli_main._format_timeline_detail(detail) == (
        "reason=needs_human summary=Custom count=2"
    )


def test_format_timeline_detail_formats_unknown_reason_without_summary() -> None:
    assert cli_main._format_timeline_detail({"reason": "custom", "count": 1}) == (
        "reason=custom count=1"
    )


def test_restart_internal_run_loop_execs_module_with_remaining_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_execve(
        executable: str,
        args: list[str],
        env: dict[str, str],
    ) -> NoReturn:
        captured["executable"] = executable
        captured["args"] = args
        captured["env"] = env
        raise RuntimeError("execve intercepted")

    monkeypatch.setenv("JRI_REMAINING_TASKS", "old")
    monkeypatch.setattr(cli_main.os, "execve", fake_execve)

    with pytest.raises(RuntimeError, match="execve intercepted"):
        cli_main._restart_internal_run_loop(["start", "--dogfood"], remaining_tasks=2)

    assert captured["executable"] == cli_main.sys.executable
    assert captured["args"] == [
        cli_main.sys.executable,
        "-m",
        "jri",
        "start",
        "--dogfood",
    ]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["JRI_ALLOW_SELF_RESTART"] == "1"
    assert env["JRI_REMAINING_TASKS"] == "2"


def test_restart_internal_run_loop_removes_remaining_tasks_when_unbounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_env: dict[str, str] = {}

    def fake_execve(
        _executable: str,
        _args: list[str],
        env: dict[str, str],
    ) -> NoReturn:
        captured_env.update(env)
        raise RuntimeError("execve intercepted")

    monkeypatch.setenv("JRI_REMAINING_TASKS", "old")
    monkeypatch.setattr(cli_main.os, "execve", fake_execve)

    with pytest.raises(RuntimeError, match="execve intercepted"):
        cli_main._restart_internal_run_loop(["start"], remaining_tasks=None)

    assert captured_env["JRI_ALLOW_SELF_RESTART"] == "1"
    assert "JRI_REMAINING_TASKS" not in captured_env


def test_main_without_command_prints_help_and_returns_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeService:
        def __init__(self, root: Path) -> None:
            self.root = root

    monkeypatch.setattr(cli_main, "JriService", FakeService)

    assert cli_main.main([], cwd=tmp_path) == 1

    captured = capsys.readouterr()
    assert "usage: jri" in captured.out
    assert captured.err == ""


def test_main_rejects_stop_reason_with_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeService:
        def __init__(self, root: Path) -> None:
            self.root = root

    monkeypatch.setattr(cli_main, "JriService", FakeService)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["stop", "--cancel", "because"], cwd=tmp_path)

    assert exc_info.value.code == 2
    assert "unrecognized arguments: reason" in capsys.readouterr().err


def test_main_runs_attach_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, bool] = {}

    class FakeService:
        def __init__(self, root: Path) -> None:
            self.root = root

        def attach(self) -> None:
            calls["attach"] = True

    monkeypatch.setattr(cli_main, "JriService", FakeService)

    assert cli_main.main(["attach"], cwd=tmp_path) == 0
    assert calls == {"attach": True}


def test_main_reports_detached_start_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeService:
        def __init__(self, root: Path) -> None:
            self.root = root

        def start(self, **_: object) -> int:
            return -4

    monkeypatch.setattr(cli_main, "JriService", FakeService)

    assert cli_main.main(["start", "--detached"], cwd=tmp_path) == 4
    assert "start: command exited with status 4" in capsys.readouterr().err


def test_main_reset_prompt_includes_discard_and_branch_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeGit:
        def status_short(self) -> list[str]:
            return ["M README.md"]

    calls: dict[str, object] = {}

    class FakeService:
        def __init__(self, root: Path) -> None:
            self.root = root
            self.git = FakeGit()

        def resolve_reset_target_point(self, task: str | None) -> str:
            calls["reset_point"] = task
            return "task-1 completion"

        def has_managed_ralph_branch(self) -> bool:
            return True

        def describe_reset_target(self, reset_point: str) -> str:
            calls["describe_reset_target"] = reset_point
            return reset_point

        def reset(self, *, target_task: str | None = None) -> None:
            calls["reset_task"] = target_task

    monkeypatch.setattr(cli_main, "JriService", FakeService)
    monkeypatch.setattr("builtins.input", lambda: "y")

    assert cli_main.main(["reset", "task-1"], cwd=tmp_path) == 0

    captured = capsys.readouterr()
    assert "Uncommitted changes will be discarded." in captured.out
    assert "The Ralph worktree branch and worktree will be deleted." in captured.out
    assert calls["reset_point"] == "task-1"
    assert calls["describe_reset_target"] == "task-1 completion"
    assert calls["reset_task"] == "task-1"


def test_main_reset_prompt_without_branch_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeGit:
        def status_short(self) -> list[str]:
            return []

    calls: dict[str, object] = {}

    class FakeService:
        def __init__(self, root: Path) -> None:
            self.root = root
            self.git = FakeGit()

        def resolve_reset_target_point(self, task: str | None) -> str:
            calls["reset_point"] = task
            return "task-3 completion"

        def has_managed_ralph_branch(self) -> bool:
            return False

        def describe_reset_target(self, reset_point: str) -> str:
            calls["describe_reset_target"] = reset_point
            return reset_point

        def reset(self, *, target_task: str | None = None) -> None:
            calls["reset_task"] = target_task

    monkeypatch.setattr(cli_main, "JriService", FakeService)
    monkeypatch.setattr("builtins.input", lambda: "y")

    assert cli_main.main(["reset", "task-3"], cwd=tmp_path) == 0

    captured = capsys.readouterr()
    assert "The Ralph worktree branch and worktree will be deleted." not in captured.out
    assert "Are you sure? [y/N]" in captured.out
    assert calls["reset_point"] == "task-3"
    assert calls["describe_reset_target"] == "task-3 completion"
    assert calls["reset_task"] == "task-3"


def test_main_reset_aborts_when_confirmation_input_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeGit:
        def status_short(self) -> list[str]:
            return []

    calls: dict[str, object] = {}

    class FakeService:
        def __init__(self, root: Path) -> None:
            self.root = root
            self.git = FakeGit()

        def resolve_reset_target_point(self, task: str | None) -> str:
            calls["reset_point"] = task
            return "task-1 completion"

        def has_managed_ralph_branch(self) -> bool:
            return False

        def describe_reset_target(self, reset_point: str) -> str:
            return reset_point

        def reset(self, *, target_task: str | None = None) -> None:
            calls["reset_task"] = target_task

    def closed_input() -> str:
        raise EOFError

    monkeypatch.setattr(cli_main, "JriService", FakeService)
    monkeypatch.setattr("builtins.input", closed_input)

    assert cli_main.main(["reset"], cwd=tmp_path) == 1

    captured = capsys.readouterr()
    assert "Are you sure? [y/N]" in captured.out
    assert "Reset aborted." in captured.err
    assert "reset_task" not in calls


def test_main_reset_force_skips_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeService:
        def __init__(self, root: Path) -> None:
            self.root = root

        def resolve_reset_target_point(self, task: str | None) -> str:
            calls["reset_point"] = task
            return "task-2 completion"

        def has_managed_ralph_branch(self) -> bool:
            return False

        def describe_reset_target(self, reset_point: str) -> str:
            calls["describe_reset_target"] = reset_point
            return reset_point

        def reset(self, *, target_task: str | None = None) -> None:
            calls["reset_task"] = target_task

    monkeypatch.setattr(cli_main, "JriService", FakeService)

    assert cli_main.main(["reset", "--force", "task-2"], cwd=tmp_path) == 0
    assert calls["reset_task"] == "task-2"


def test_main_translates_called_process_error_with_string_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeService:
        def __init__(self, root: Path) -> None:
            self.root = root

        def init(self, **_: object) -> None:
            raise subprocess.CalledProcessError(
                returncode=1, cmd="git status", stderr=""
            )

    monkeypatch.setattr(cli_main, "JriService", FakeService)

    assert cli_main.main(["init"], cwd=tmp_path) == 1

    captured = capsys.readouterr()
    assert "git command failed: git status" in captured.err
    assert "boom" not in captured.err


def test_main_translates_called_process_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeService:
        def __init__(self, root: Path) -> None:
            self.root = root

        def init(self, **_: object) -> None:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=["git", "commit"],
                stderr="boom\n",
            )

    monkeypatch.setattr(cli_main, "JriService", FakeService)

    assert cli_main.main(["init"], cwd=tmp_path) == 1

    captured = capsys.readouterr()
    assert "git command failed: git commit" in captured.err
    assert "boom" in captured.err
