import json
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import Any, cast

import pytest

from jri.cli.main import main, resolve_start_models
from jri.core.agents import PiRuntime
from jri.core.errors import HaltRequested, JriError, RestartRequested
from jri.core.git import MSG_RECOVER_STALE
from jri.core.models import (
    AgentRunResult,
    AttemptState,
    HumanTaskPayload,
    ProcessState,
    PromotionRecord,
    RalphResultPayload,
    RunSummary,
    State,
    Task,
)
from jri.core.service import JriService, _FollowControls
from jri.core.tasks import list_tasks, parse_task_file
from jri.core.ui import CYAN, RESET
from tests.conftest import run_cli as base_run_cli
from tests.helpers import git, read_json, write_passing_makefile, write_task


def run_cli(args: list[str], cwd: Path) -> int:
    exit_code = base_run_cli(args, cwd=cwd)
    if args == ["init"] and exit_code == 0:
        write_passing_makefile(cwd)
        git(cwd, "add", "Makefile")
        git(cwd, "commit", "-m", "configure check")
    return exit_code


class FakeAgentRuntime:
    def __init__(self, *, model: str | None = None) -> None:
        self.model = model

    def list_sessions(self, *, root: Path, limit: int = 20) -> list[dict[str, object]]:
        return []

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        raise NotImplementedError

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text("{}\n", encoding="utf-8")


class SuccessfulFakeAgentRuntime(FakeAgentRuntime):
    def __init__(self) -> None:
        super().__init__(model=None)
        self.calls: list[tuple[str, Path]] = []
        self.models_used: list[str | None] = []

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        self.calls.append((prompt, log_path))
        self.models_used.append(self.model)
        (root / "implemented.txt").write_text("implemented\n", encoding="utf-8")
        log_path.write_text("fake run\n", encoding="utf-8")
        return AgentRunResult(
            returncode=0,
            session_id="ses_fake",
            result="completed",
            payload=RalphResultPayload(
                result="completed",
                summary="Completed the requested work.",
                learnings=["Keep the implementation minimal."],
            ),
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text('{"session": "fake"}\n', encoding="utf-8")


class NeedsHumanFakeAgentRuntime(FakeAgentRuntime):
    """Simulates Ralph resolving the task as needs human."""

    def __init__(self) -> None:
        super().__init__(model=None)
        self.calls: list[tuple[str, Path]] = []

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        self.calls.append((prompt, log_path))
        log_path.write_text("fake needs-human run\n", encoding="utf-8")
        return AgentRunResult(
            returncode=0,
            session_id="ses_needs_human",
            result="needs_human",
            payload=RalphResultPayload(
                result="needs_human",
                blocker="A human action is required.",
                human_task=HumanTaskPayload(
                    title="Provide missing input",
                    body="A human must provide the missing input.",
                    acceptance_criteria=["Required input is provided"],
                ),
            ),
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text('{"session": "fake_needs_human"}\n', encoding="utf-8")


class NeedsHumanThenSuccessfulFakeAgentRuntime(FakeAgentRuntime):
    """Returns needs human for the first call, successful for the second."""

    def __init__(self) -> None:
        super().__init__(model=None)
        self.calls: list[tuple[str, Path]] = []
        self._call_count = 0

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        self.calls.append((prompt, log_path))
        self._call_count += 1
        log_path.write_text(f"fake run #{self._call_count}\n", encoding="utf-8")
        if self._call_count == 1:
            return AgentRunResult(
                returncode=0,
                session_id="ses_needs_human",
                result="needs_human",
                payload=RalphResultPayload(
                    result="needs_human",
                    blocker="A human action is required.",
                    human_task=HumanTaskPayload(
                        title="Provide missing input",
                        body="A human must provide the missing input.",
                        acceptance_criteria=["Required input is provided"],
                    ),
                ),
            )
        (root / "implemented.txt").write_text("implemented\n", encoding="utf-8")
        return AgentRunResult(returncode=0, session_id="ses_ok", result="completed")

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text('{"session": "fake"}\n', encoding="utf-8")


class MissingDoingTaskAgentRuntime(SuccessfulFakeAgentRuntime):
    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        (root / ".jri" / "tasks" / "doing" / "implement-file.md").unlink()
        return super().run_ralph_task(
            root=root,
            prompt=prompt,
            log_path=log_path,
            result_path=result_path,
            on_start=on_start,
            timeout=timeout,
        )


class MutatingDoingTaskAgentRuntime(SuccessfulFakeAgentRuntime):
    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        doing_path = root / ".jri" / "tasks" / "doing" / "implement-file.md"
        doing_path.write_text(
            doing_path.read_text(encoding="utf-8") + "\nMutated in place.\n",
            encoding="utf-8",
        )
        return super().run_ralph_task(
            root=root,
            prompt=prompt,
            log_path=log_path,
            result_path=result_path,
            on_start=on_start,
            timeout=timeout,
        )


class CommittedMutatingDoingTaskAgentRuntime(SuccessfulFakeAgentRuntime):
    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        doing_path = root / ".jri" / "tasks" / "doing" / "implement-file.md"
        doing_path.write_text(
            doing_path.read_text(encoding="utf-8") + "\nCommitted mutation.\n",
            encoding="utf-8",
        )
        git(root, "add", ".jri/tasks/doing/implement-file.md")
        git(root, "commit", "-m", "mutate task in place")
        return super().run_ralph_task(
            root=root,
            prompt=prompt,
            log_path=log_path,
            result_path=result_path,
            on_start=on_start,
            timeout=timeout,
        )


class FollowUpDraftAgentRuntime(SuccessfulFakeAgentRuntime):
    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        write_task(
            root,
            status="draft",
            slug="follow-up-fix",
            title="Follow up fix",
            priority=1,
            assignee="Ralph",
            body="Capture the additive follow-up work.\n",
            acceptance_criteria=["Follow-up task is triaged"],
        )
        return super().run_ralph_task(
            root=root,
            prompt=prompt,
            log_path=log_path,
            result_path=result_path,
            on_start=on_start,
            timeout=timeout,
        )


class InterruptedStartupPiRuntime(PiRuntime):
    def __init__(self) -> None:
        super().__init__(binary="pi")
        self.stop_calls = 0

    def start(
        self,
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        self._process = cast(Any, FakeDetachedProcess(989898))
        raise HaltRequested("Ralph halt requested")

    def stop(self) -> None:
        self.stop_calls += 1
        self._process = None


class CapturingStartupPiRuntime(InterruptedStartupPiRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.started_env: dict[str, str] | None = None
        self.manifest_text: str | None = None
        self.package_root: Path | None = None

    def start(
        self,
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        assert env is not None
        self.started_env = env
        self.package_root = Path(env["JRI_PI_PACKAGE"])
        self.manifest_text = (self.package_root / "package.json").read_text(
            encoding="utf-8"
        )
        super().start(env=env, cwd=cwd)


class RefreshCapturingPiRuntime(PiRuntime):
    def __init__(self) -> None:
        super().__init__(binary="pi")
        self.start_package_roots: list[Path] = []
        self.stop_calls = 0

    def is_healthy(self) -> bool:
        return True

    def list_sessions(self, *, root: Path, limit: int = 20) -> list[dict[str, object]]:
        return []

    def start(
        self,
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        assert env is not None
        self.start_package_roots.append(Path(env["JRI_PI_PACKAGE"]))
        self._process = cast(
            Any,
            FakeDetachedProcess(7000 + len(self.start_package_roots)),
        )

    def stop(self) -> None:
        self.stop_calls += 1
        self._process = None

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        del result_path, timeout
        if on_start is not None and self._process is not None:
            cast(Any, on_start)(self._process.pid)
        slug = _extract_task_slug(prompt)
        (root / f"{slug}.txt").write_text(f"{slug}\n", encoding="utf-8")
        log_path.write_text(f"completed {slug}\n", encoding="utf-8")
        return AgentRunResult(
            returncode=0,
            session_id=f"ses_{slug}",
            result="completed",
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text(f'{{"session": "{session_id}"}}\n', encoding="utf-8")


class FakeDetachedProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> None:
        return None

    def wait(self) -> int:
        return 0


def _dead_pid() -> int:
    process = subprocess.Popen(["sleep", "0"])
    process.wait(timeout=5)
    return process.pid


def _extract_task_slug(prompt: str) -> str:
    match = re.search(r"\.jri/tasks/doing/([^/]+)\.md", prompt)
    return match.group(1) if match else ""


def test_start_uses_explicit_model_override(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    client = SuccessfulFakeAgentRuntime()
    service = JriService(git_repo, agent_runtime=client)

    completed = service.start(
        max_tasks=1, model="vercel/alibaba/qwen3.6-plus", force=True
    )

    assert completed == 1
    assert client.models_used == ["vercel/alibaba/qwen3.6-plus"]
    assert client.model is None


def test_start_model_overrides_use_temporary_pi_package(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0

    runtime = CapturingStartupPiRuntime()
    service = JriService(git_repo, agent_runtime=runtime)

    with pytest.raises(HaltRequested, match="Ralph halt requested"):
        service.run_loop_process(
            max_tasks=1,
            model="provider/ralph-main",
            validator_model="provider/ralph-validator",
            general_model="provider/general-subagent",
            explore_model="provider/explore-subagent",
            force=True,
        )

    assert runtime.started_env is not None
    assert runtime.package_root is not None
    assert runtime.manifest_text is not None
    assert Path(runtime.started_env["JRI_PI_PACKAGE"]) == runtime.package_root
    assert runtime.started_env["JRI_PYTHON"]
    assert Path(runtime.started_env["JRI_PYTHONPATH"]).exists()
    assert not runtime.package_root.is_relative_to(git_repo)
    assert '"ralph": "provider/ralph-main"' in runtime.manifest_text
    assert '"ralph-validator": "provider/ralph-validator"' in runtime.manifest_text
    assert '"explore": "provider/explore-subagent"' in runtime.manifest_text
    assert '"general": "provider/general-subagent"' in runtime.manifest_text
    assert not runtime.package_root.exists()


def test_start_refreshes_pi_runtime_each_iteration_in_dogfood_mode(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    for slug in ("task-a", "task-b"):
        write_task(
            git_repo,
            status="todo",
            slug=slug,
            title=slug,
            priority=0,
            assignee="Ralph",
            body=f"Complete {slug}.",
            acceptance_criteria=[f"{slug}.txt exists"],
        )
    git(git_repo, "add", ".jri/tasks/todo/")
    git(git_repo, "commit", "-m", "add self-hosting tasks")

    runtime = RefreshCapturingPiRuntime()
    service = JriService(git_repo, agent_runtime=runtime)

    completed = service.start(max_tasks=2, force=True, dogfood=True)

    assert completed == 2
    assert len(runtime.start_package_roots) == 2
    assert runtime.start_package_roots[0] != runtime.start_package_roots[1]
    assert runtime.stop_calls == 2


def test_start_keeps_single_pi_runtime_without_dogfood(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    for slug in ("task-a", "task-b"):
        write_task(
            git_repo,
            status="todo",
            slug=slug,
            title=slug,
            priority=0,
            assignee="Ralph",
            body=f"Complete {slug}.",
            acceptance_criteria=[f"{slug}.txt exists"],
        )
    git(git_repo, "add", ".jri/tasks/todo/")
    git(git_repo, "commit", "-m", "add regular tasks")

    runtime = RefreshCapturingPiRuntime()
    service = JriService(git_repo, agent_runtime=runtime)

    completed = service.start(max_tasks=2, force=True)

    assert completed == 2
    assert len(runtime.start_package_roots) == 1
    assert runtime.stop_calls == 1


def test_start_detached_passes_validator_model_to_child(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    popen_calls: list[list[str]] = []
    popen_envs: list[dict[str, str]] = []

    def fake_popen(*args: object, **kwargs: object) -> object:
        command = cast(list[str], args[0])
        popen_calls.append(command)
        popen_envs.append(cast(dict[str, str], kwargs["env"]))
        assert kwargs["cwd"] == git_repo
        assert kwargs["start_new_session"] is True
        return FakeDetachedProcess(424242)

    monkeypatch.setattr("jri.core.service.subprocess.Popen", fake_popen)

    assert (
        service._start_detached(
            1,
            "provider/ralph-main",
            "provider/ralph-validator",
            "provider/general-subagent",
            "provider/explore-subagent",
            None,
            True,
        )
        == 0
    )
    assert len(popen_calls) == 1
    command = popen_calls[0]
    assert command[:3] == [sys.executable, "-m", "jri"]
    assert popen_envs[0]["JRI_INTERNAL_RUN_LOOP"] == "1"
    assert "--model" in command
    assert "provider/ralph-main" in command
    assert "--validator-model" in command
    assert "provider/ralph-validator" in command
    assert "--general-model" in command
    assert "provider/general-subagent" in command
    assert "--explore-model" in command
    assert "provider/explore-subagent" in command
    assert "--dogfood" in command


def test_internal_run_loop_cli_passes_subagent_model_overrides(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    captured: dict[str, object] = {}

    def fake_run_loop_process(
        self: JriService,
        *,
        max_tasks: int | None = None,
        model: str | None = None,
        validator_model: str | None = None,
        general_model: str | None = None,
        explore_model: str | None = None,
        task_timeout: int | None = None,
        force: bool = False,
        recover: bool = False,
        mode: str = "foreground",
        dogfood: bool = False,
    ) -> int:
        captured.update(
            {
                "max_tasks": max_tasks,
                "model": model,
                "validator_model": validator_model,
                "general_model": general_model,
                "explore_model": explore_model,
                "task_timeout": task_timeout,
                "force": force,
                "recover": recover,
                "mode": mode,
                "dogfood": dogfood,
            }
        )
        return 1

    monkeypatch.setattr(JriService, "run_loop_process", fake_run_loop_process)
    monkeypatch.setenv("JRI_INTERNAL_RUN_LOOP", "1")

    result = main(
        [
            "--tasks",
            "2",
            "--model",
            "provider/ralph-main",
            "--validator-model",
            "provider/ralph-validator",
            "--general-model",
            "provider/general-subagent",
            "--explore-model",
            "provider/explore-subagent",
            "--task-timeout",
            "60",
            "--force",
        ],
        cwd=git_repo,
    )

    assert result == 0
    assert captured == {
        "max_tasks": 2,
        "model": "provider/ralph-main",
        "validator_model": "provider/ralph-validator",
        "general_model": "provider/general-subagent",
        "explore_model": "provider/explore-subagent",
        "task_timeout": 60,
        "force": True,
        "recover": False,
        "mode": "foreground",
        "dogfood": False,
    }


def test_ctl_start_help_accepts_validator_model_flag(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0

    with pytest.raises(SystemExit) as exc_info:
        run_cli(["start", "--help"], cwd=git_repo)

    assert exc_info.value.code == 0


def test_ctl_start_help_includes_preset_flag(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0

    with pytest.raises(SystemExit) as exc_info:
        run_cli(["start", "--help"], cwd=git_repo)

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "-p {default,openai}" in help_text
    assert "--preset {default,openai}" in help_text


def test_start_cli_preset_sets_models(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    captured: dict[str, object] = {}

    def fake_start_attached(
        self: JriService,
        *,
        max_tasks: int | None = None,
        model: str | None = None,
        validator_model: str | None = None,
        general_model: str | None = None,
        explore_model: str | None = None,
        task_timeout: int | None = None,
        force: bool = False,
        dogfood: bool = False,
    ) -> int:
        captured.update(
            {
                "max_tasks": max_tasks,
                "model": model,
                "validator_model": validator_model,
                "general_model": general_model,
                "explore_model": explore_model,
                "task_timeout": task_timeout,
                "force": force,
                "dogfood": dogfood,
            }
        )
        return 0

    monkeypatch.setattr(JriService, "start_attached", fake_start_attached)

    result = main(["start", "-p", "openai", "--tasks", "2"], cwd=git_repo)

    assert result == 0
    assert captured == {
        "max_tasks": 2,
        "model": "openai-codex/gpt-5.4",
        "validator_model": "openai-codex/gpt-5.4",
        "general_model": "openai-codex/gpt-5.3-codex",
        "explore_model": "openai-codex/gpt-5.4-mini",
        "task_timeout": None,
        "force": False,
        "dogfood": False,
    }


def test_start_cli_explicit_model_overrides_preset(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    captured: dict[str, object] = {}

    def fake_start_attached(
        self: JriService,
        *,
        max_tasks: int | None = None,
        model: str | None = None,
        validator_model: str | None = None,
        general_model: str | None = None,
        explore_model: str | None = None,
        task_timeout: int | None = None,
        force: bool = False,
        dogfood: bool = False,
    ) -> int:
        captured.update(
            {
                "max_tasks": max_tasks,
                "model": model,
                "validator_model": validator_model,
                "general_model": general_model,
                "explore_model": explore_model,
                "task_timeout": task_timeout,
                "force": force,
                "dogfood": dogfood,
            }
        )
        return 0

    monkeypatch.setattr(JriService, "start_attached", fake_start_attached)

    result = main(
        [
            "start",
            "-p",
            "openai",
            "--general-model",
            "openai/gpt-5.4",
        ],
        cwd=git_repo,
    )

    assert result == 0
    assert captured == {
        "max_tasks": None,
        "model": "openai-codex/gpt-5.4",
        "validator_model": "openai-codex/gpt-5.4",
        "general_model": "openai/gpt-5.4",
        "explore_model": "openai-codex/gpt-5.4-mini",
        "task_timeout": None,
        "force": False,
        "dogfood": False,
    }


def test_resolve_start_models_applies_preset_and_explicit_overrides() -> None:
    assert resolve_start_models(
        preset="openai",
        model=None,
        validator_model="openai/gpt-5.4-mini",
        general_model=None,
        explore_model=None,
    ) == {
        "model": "openai-codex/gpt-5.4",
        "validator_model": "openai/gpt-5.4-mini",
        "general_model": "openai-codex/gpt-5.3-codex",
        "explore_model": "openai-codex/gpt-5.4-mini",
    }


def test_top_level_help_lists_commands_alphabetically(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0

    result = subprocess.run(
        [sys.executable, "-m", "jri", "--help"],
        cwd=git_repo,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0

    command_positions = [
        result.stdout.index("attach"),
        result.stdout.index("chat"),
        result.stdout.index("halt"),
        result.stdout.index("init"),
        result.stdout.index("inspect"),
        result.stdout.index("reset"),
        result.stdout.index("start"),
        result.stdout.index("status"),
        result.stdout.index("stop"),
        result.stdout.index("timeline"),
    ]
    assert command_positions == sorted(command_positions)


def test_ctl_run_loop_is_not_a_public_command(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0

    with pytest.raises(SystemExit) as exc_info:
        run_cli(["_run-loop"], cwd=git_repo)

    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_unknown_args_are_rejected_outside_chat(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        base_run_cli(["status", "--bogus"], cwd=git_repo)

    assert exc_info.value.code == 2
    assert "unrecognized arguments: --bogus" in capsys.readouterr().err


def test_chat_preserves_unknown_args_for_agent_runtime(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_chat(
        self: JriService,
        extra_args: list[str],
        *,
        fresh: bool = False,
        model: str | None = None,
        validator_model: str | None = None,
        explore_model: str | None = None,
    ) -> int:
        captured.update(
            {
                "root": self.root,
                "extra_args": extra_args,
                "fresh": fresh,
                "model": model,
                "validator_model": validator_model,
                "explore_model": explore_model,
            }
        )
        return 0

    monkeypatch.setattr(JriService, "chat", fake_chat)

    result = base_run_cli(
        ["chat", "--fresh", "--", "--agent-flag", "value"],
        cwd=git_repo,
    )

    assert result == 0
    assert captured == {
        "root": git_repo.resolve(),
        "extra_args": ["--", "--agent-flag", "value"],
        "fresh": True,
        "model": None,
        "validator_model": None,
        "explore_model": None,
    }


def test_internal_run_loop_uses_remaining_task_budget(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run_loop_process(self: JriService, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setenv("JRI_INTERNAL_RUN_LOOP", "1")
    monkeypatch.setenv("JRI_REMAINING_TASKS", "1")
    monkeypatch.setattr(JriService, "run_loop_process", fake_run_loop_process)

    assert base_run_cli(["-n", "2", "--force", "--dogfood"], cwd=git_repo) == 0
    assert captured["max_tasks"] == 1
    assert captured["dogfood"] is True


def test_internal_run_loop_reexecs_after_restart_request(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class ExecveCalled(Exception):
        pass

    def fake_run_loop_process(self: JriService, **kwargs: object) -> int:
        del kwargs
        raise RestartRequested(remaining_tasks=1)

    def fake_execve(path: str, args: list[str], env: dict[str, str]) -> None:
        captured["path"] = path
        captured["args"] = args
        captured["env"] = env
        raise ExecveCalled

    monkeypatch.setenv("JRI_INTERNAL_RUN_LOOP", "1")
    monkeypatch.delenv("JRI_REMAINING_TASKS", raising=False)
    monkeypatch.setattr(JriService, "run_loop_process", fake_run_loop_process)
    cli_main = import_module("jri.cli.main")
    monkeypatch.setattr(cli_main.os, "execve", fake_execve)

    with pytest.raises(ExecveCalled):
        base_run_cli(["-n", "2", "--force", "--dogfood"], cwd=git_repo)

    assert captured["path"] == sys.executable
    assert captured["args"] == [
        sys.executable,
        "-m",
        "jri",
        "-n",
        "2",
        "--force",
        "--dogfood",
    ]
    env = cast(dict[str, str], captured["env"])
    assert env["JRI_ALLOW_SELF_RESTART"] == "1"
    assert env["JRI_INTERNAL_RUN_LOOP"] == "1"
    assert env["JRI_REMAINING_TASKS"] == "1"


def test_start_completes_single_task(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    completed = service.start(max_tasks=1, force=True)

    assert completed == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "implement-file.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "todo" / "implement-file.md").exists()
    assert (git_repo / "implemented.txt").read_text(encoding="utf-8") == "implemented\n"
    assert git(git_repo, "branch", "--show-current") == "main"
    tags = git(git_repo, "tag").splitlines()
    assert "jri/begin/implement-file" in tags
    assert "jri/end/implement-file" in tags
    attempts = cast(
        list[dict[str, object]],
        read_json(git_repo / ".jri" / "state.json")["attempts"],
    )
    assert len(attempts) == 1
    assert attempts[0]["number"] == 1
    assert attempts[0]["task_slug"] == "implement-file"
    assert attempts[0]["branch"] == "ralph/main"
    assert attempts[0]["result"] == "completed"
    assert attempts[0]["session_id"] == "ses_fake"
    assert attempts[0]["result_payload"] == {
        "result": "completed",
        "summary": "Completed the requested work.",
        "learnings": ["Keep the implementation minimal."],
    }
    assert git(git_repo, "rev-parse", "--verify", "refs/heads/ralph/main")
    attempt_history = read_json(git_repo / ".jri" / "attempts" / "implement-file.json")
    assert attempt_history["task_slug"] == "implement-file"
    history_attempts = cast(list[dict[str, object]], attempt_history["attempts"])
    assert len(history_attempts) == 1
    assert history_attempts[0]["result_payload"] == attempts[0]["result_payload"]
    assert git(git_repo, "status", "--short") == ""


def test_start_passes_doing_task_path_to_ralph(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    client = SuccessfulFakeAgentRuntime()
    service = JriService(git_repo, agent_runtime=client)

    assert service.start(max_tasks=1, force=True) == 1
    assert len(client.calls) == 1
    assert (
        client.calls[0][0]
        == "Solve `.jri/tasks/doing/implement-file.md`. Commit frequently. "
        "Stay on the Ralph worktree/branch; the runtime handles integration, so do "
        "not merge to the default branch yourself."
    )


def test_start_interrupt_during_pi_start_stops_pi(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0

    runtime = InterruptedStartupPiRuntime()
    service = JriService(git_repo, agent_runtime=runtime)

    with pytest.raises(HaltRequested, match="Ralph halt requested"):
        service.start(max_tasks=1, force=True)

    assert runtime.stop_calls == 1
    state = read_json(git_repo / ".jri" / "state.json")
    assert state.get("process") is None


def test_start_fails_cleanly_when_doing_task_disappears(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    service = JriService(git_repo, agent_runtime=MissingDoingTaskAgentRuntime())

    with pytest.raises(JriError, match="disappeared during Ralph run"):
        service.start(max_tasks=1, force=True)


def test_start_restores_in_place_mutation_of_doing_task(git_repo: Path) -> None:
    """In-place modifications to the doing task file are silently restored.

    Project tooling (prettier, eslint, etc.) may touch the task file as a
    side effect. The runtime restores it to baseline rather than failing the run.
    """
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    service = JriService(git_repo, agent_runtime=MutatingDoingTaskAgentRuntime())

    completed = service.start(max_tasks=1, force=True)
    assert completed == 1
    # Task file is in done/ with original content (mutation was restored)
    done_path = git_repo / ".jri" / "tasks" / "done" / "implement-file.md"
    assert done_path.exists()
    assert "Create implemented.txt" in done_path.read_text(encoding="utf-8")


def test_start_restores_committed_in_place_mutation_of_doing_task(
    git_repo: Path,
) -> None:
    """Same as above, but the mutation was committed by Ralph.

    The runtime restores the working-tree file to baseline; the committed
    mutation is harmless because the file gets overwritten.
    """
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    service = JriService(
        git_repo,
        agent_runtime=CommittedMutatingDoingTaskAgentRuntime(),
    )

    completed = service.start(max_tasks=1, force=True)
    assert completed == 1
    done_path = git_repo / ".jri" / "tasks" / "done" / "implement-file.md"
    assert done_path.exists()
    assert "Create implemented.txt" in done_path.read_text(encoding="utf-8")


def test_start_allows_additive_follow_up_draft_tasks(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    service = JriService(git_repo, agent_runtime=FollowUpDraftAgentRuntime())

    completed = service.start(max_tasks=1, force=True)

    assert completed == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "implement-file.md").exists()
    follow_up = parse_task_file(
        git_repo / ".jri" / "tasks" / "draft" / "follow-up-fix.md"
    )
    assert follow_up.metadata.title == "Follow up fix"
    assert "additive follow-up" in follow_up.body


def test_start_refuses_when_tracked_process_is_still_alive(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="existing",
        title="Existing",
        priority=1,
        assignee="Ralph",
        body="Already running.",
    )
    sleeper = subprocess.Popen(["sleep", "30"])

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save_process(
        loop_pid=sleeper.pid,
        child_pid=None,
        log_path=None,
        detached=False,
    )

    try:
        with pytest.raises(JriError, match="already running"):
            service.start(max_tasks=1, force=True)
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)


def test_ctl_start_suggests_attach_when_tracked_process_is_still_alive(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    capsys.readouterr()
    write_task(
        git_repo,
        status="doing",
        slug="existing",
        title="Existing",
        priority=1,
        assignee="Ralph",
        body="Already running.",
    )
    sleeper = subprocess.Popen(["sleep", "30"])

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save_process(
        loop_pid=sleeper.pid,
        child_pid=None,
        log_path=None,
        detached=False,
    )

    try:
        assert run_cli(["start", "-n", "1", "--force"], cwd=git_repo) == 1
        assert (
            "start: a Ralph process is already running; use `jri attach` to follow it"
            in capsys.readouterr().err
        )
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)


def test_start_rejects_multiple_doing_tasks_at_start(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    for slug in ("task-a", "task-b"):
        write_task(
            git_repo,
            status="doing",
            slug=slug,
            title=slug.replace("-", " ").title(),
            priority=0,
            assignee="Ralph",
            body="In progress.",
            acceptance_criteria=["Task is ready to continue"],
        )
    git(git_repo, "add", ".jri/tasks/doing")
    git(git_repo, "commit", "-m", "seed multiple doing tasks")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    with pytest.raises(JriError, match="multiple tasks are already in progress"):
        service.start(max_tasks=1, force=True)


def test_start_rejects_active_attempt_task_slug_mismatch(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/doing/implement-file.md")
    git(git_repo, "commit", "-m", "seed mismatched active attempt")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save(
        State(
            active_attempt=AttemptState(
                number=1,
                task_slug="other-task",
                branch="ralph",
                started_at=123,
            ),
            attempts=[
                AttemptState(
                    number=1,
                    task_slug="other-task",
                    branch="ralph",
                    started_at=123,
                )
            ],
        )
    )

    with pytest.raises(
        JriError, match="active attempt does not match the task in progress"
    ):
        service.start(max_tasks=1, force=True)


def test_start_recovers_clean_foreground_interruption(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/doing/implement-file.md")
    git(git_repo, "commit", "-m", "seed interrupted task")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    completed = service.start(max_tasks=1, force=True)

    assert completed == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "implement-file.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "implement-file.md").exists()
    recovery_log = (git_repo / ".jri" / "logs" / "recovery.log").read_text(
        encoding="utf-8"
    )
    assert "mode=foreground" in recovery_log
    assert "task=implement-file" in recovery_log
    assert "reason=no-tracked-process" in recovery_log
    history = git(git_repo, "log", "--oneline", "--decorate=short", "-5")
    assert MSG_RECOVER_STALE.format(slug="implement-file") in history


def test_start_records_retry_attempt_after_interrupted_run(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/doing/implement-file.md")
    git(git_repo, "commit", "-m", "seed interrupted attempt")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    interrupted_attempt = AttemptState(
        number=1,
        task_slug="implement-file",
        branch="ralph",
        started_at=123,
        log_path=".jri/logs/ralph/1-interrupted.log",
    )
    service.state_store.save(
        State(
            started_at=123,
            branch="main",
            active_attempt=interrupted_attempt,
            attempts=[interrupted_attempt],
        )
    )

    completed = service.start(max_tasks=1, force=True)

    assert completed == 1
    attempts = cast(
        list[dict[str, object]],
        read_json(git_repo / ".jri" / "state.json")["attempts"],
    )
    assert [attempt["number"] for attempt in attempts] == [1, 2]
    assert [attempt["task_slug"] for attempt in attempts] == [
        "implement-file",
        "implement-file",
    ]
    assert attempts[0]["result"] == "interrupted"
    assert attempts[1]["result"] == "completed"


def test_start_reruns_unverified_completed_attempt(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="done",
        slug="recover-me",
        title="Recover me",
        priority=0,
        assignee="Ralph",
        body="This task was incorrectly promoted without durable evidence.",
        acceptance_criteria=["implemented.txt exists"],
    )
    write_task(
        git_repo,
        status="todo",
        slug="next-task",
        title="Next task",
        priority=1,
        assignee="Ralph",
        body="This should not start until recover-me is really complete.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(
        git_repo,
        "add",
        ".jri/tasks/done/recover-me.md",
        ".jri/tasks/todo/next-task.md",
    )
    git(git_repo, "commit", "-m", "seed unverified completed attempt")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    stale_attempt = AttemptState(
        number=1,
        task_slug="recover-me",
        branch="ralph",
        started_at=123,
        finished_at=124,
        log_path=".jri/logs/ralph/recover-me-1970-01-01T00-02-03Z.log",
        result="completed",
    )
    service.state_store.save(
        State(
            started_at=123,
            branch="main",
            active_attempt=stale_attempt,
            attempts=[stale_attempt],
        )
    )

    completed = service.start(max_tasks=1, force=True)

    assert completed == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "recover-me.md").exists()
    assert (git_repo / ".jri" / "tasks" / "todo" / "next-task.md").exists()

    attempts = cast(
        list[dict[str, object]],
        read_json(git_repo / ".jri" / "state.json")["attempts"],
    )
    assert [attempt["task_slug"] for attempt in attempts] == [
        "recover-me",
        "recover-me",
    ]
    assert attempts[0]["result"] == "interrupted"
    assert attempts[1]["result"] == "completed"

    recovery_log = (git_repo / ".jri" / "logs" / "recovery.log").read_text(
        encoding="utf-8"
    )
    assert "reason=missing-completion-evidence" in recovery_log
    assert "reason=resume-completed-attempt" not in recovery_log


def test_start_does_not_resume_completed_attempt_from_timeline_event_only(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="recover-me",
        title="Recover me",
        priority=0,
        assignee="Ralph",
        body=(
            "This task needs durable completion evidence before recovery can resume it."
        ),
        acceptance_criteria=["implemented.txt exists"],
    )
    write_task(
        git_repo,
        status="todo",
        slug="next-task",
        title="Next task",
        priority=1,
        assignee="Ralph",
        body="This should remain queued until recover-me is durably complete.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(
        git_repo,
        "add",
        ".jri/tasks/doing/recover-me.md",
        ".jri/tasks/todo/next-task.md",
    )
    git(git_repo, "commit", "-m", "seed timeline-only completion evidence")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    stale_attempt = AttemptState(
        number=1,
        task_slug="recover-me",
        branch="ralph",
        started_at=123,
        finished_at=124,
        log_path=".jri/logs/ralph/recover-me-1970-01-01T00-02-03Z.log",
        result="completed",
    )
    service.state_store.save(
        State(
            started_at=123,
            branch="main",
            active_attempt=stale_attempt,
            attempts=[stale_attempt],
        )
    )

    from jri.core.timeline import TimelineEvent, TimelineStore

    service.timeline.record(
        TimelineEvent(
            ts=TimelineStore.now_iso(),
            event="task_completed",
            task="recover-me",
        )
    )

    completed = service.start(max_tasks=1, force=True)

    assert completed == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "recover-me.md").exists()
    assert (git_repo / ".jri" / "tasks" / "todo" / "next-task.md").exists()

    attempts = cast(
        list[dict[str, object]],
        read_json(git_repo / ".jri" / "state.json")["attempts"],
    )
    assert [attempt["task_slug"] for attempt in attempts] == [
        "recover-me",
        "recover-me",
    ]
    assert attempts[0]["result"] == "interrupted"
    assert attempts[1]["result"] == "completed"

    recovery_log = (git_repo / ".jri" / "logs" / "recovery.log").read_text(
        encoding="utf-8"
    )
    assert "reason=missing-completion-evidence" in recovery_log
    assert "reason=resume-completed-attempt" not in recovery_log


def test_start_recovers_stale_foreground_process(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/doing/implement-file.md")
    git(git_repo, "commit", "-m", "seed stale process task")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save_process(
        loop_pid=_dead_pid(),
        child_pid=None,
        log_path=git_repo / ".jri" / "logs" / "ralph" / "stale.log",
        detached=False,
    )

    completed = service.start(max_tasks=1, force=True)

    assert completed == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "implement-file.md").exists()
    recovery_log = (git_repo / ".jri" / "logs" / "recovery.log").read_text(
        encoding="utf-8"
    )
    assert "mode=foreground" in recovery_log
    assert "task=implement-file" in recovery_log
    assert "reason=dead-tracked-process" in recovery_log
    history = git(git_repo, "log", "--oneline", "--decorate=short", "-6")
    assert MSG_RECOVER_STALE.format(slug="implement-file") in history


def test_start_clears_stale_process_metadata_without_doing_task(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "seed stale process metadata")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save_process(
        loop_pid=_dead_pid(),
        child_pid=None,
        log_path=git_repo / ".jri" / "logs" / "ralph" / "orphaned.log",
        detached=False,
    )

    assert service.start(max_tasks=1, force=True) == 1
    recovery_log = (git_repo / ".jri" / "logs" / "recovery.log").read_text(
        encoding="utf-8"
    )
    assert "task=-" in recovery_log
    assert "reason=dead-tracked-process" in recovery_log


def test_start_prunes_stale_worktree_metadata_before_recreating_worktree(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    for slug in ("first-task", "second-task"):
        write_task(
            git_repo,
            status="todo",
            slug=slug,
            title=slug.replace("-", " ").title(),
            priority=0,
            assignee="Ralph",
            body="Create implemented.txt with the text implemented.",
            acceptance_criteria=["implemented.txt exists"],
        )
        git(git_repo, "add", f".jri/tasks/todo/{slug}.md")
        git(git_repo, "commit", "-m", f"add {slug}")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    assert service.start(max_tasks=1, force=True) == 1

    worktree_dir = git_repo / ".jri" / "worktree"
    assert worktree_dir.exists()
    shutil.rmtree(worktree_dir)
    assert not worktree_dir.exists()

    assert service.start(max_tasks=1, force=True) == 1
    assert worktree_dir.exists()
    assert (git_repo / ".jri" / "tasks" / "done" / "second-task.md").exists()


def test_start_recovers_clean_detached_interruption(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/doing/implement-file.md")
    git(git_repo, "commit", "-m", "seed clean detached interruption")

    popen_calls: list[list[str]] = []
    original_popen = cast(Any, subprocess.Popen)
    expected_command = [sys.executable, "-m", "jri", "-n", "1"]

    def fake_popen(*args: object, **kwargs: object) -> object:
        command = cast(list[str], args[0])
        if command != expected_command:
            return original_popen(*args, **kwargs)
        popen_calls.append(command)
        assert kwargs["cwd"] == git_repo
        assert cast(dict[str, str], kwargs["env"])["JRI_INTERNAL_RUN_LOOP"] == "1"
        assert kwargs["start_new_session"] is True
        return FakeDetachedProcess(424242)

    monkeypatch.setattr("jri.core.service.subprocess.Popen", fake_popen)
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    assert service.start(max_tasks=1, detached=True, force=True) == 0
    assert popen_calls == [expected_command]
    assert (git_repo / ".jri" / "tasks" / "todo" / "implement-file.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "implement-file.md").exists()
    process = cast(
        dict[str, object], read_json(git_repo / ".jri" / "state.json")["process"]
    )
    assert process["loop_pid"] == 424242
    assert process["detached"] is True
    recovery_log = (git_repo / ".jri" / "logs" / "recovery.log").read_text(
        encoding="utf-8"
    )
    assert "mode=detached" in recovery_log
    assert "reason=no-tracked-process" in recovery_log


def test_start_recovers_stale_detached_process(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/doing/implement-file.md")
    git(git_repo, "commit", "-m", "seed stale detached process")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save_process(
        loop_pid=_dead_pid(),
        child_pid=None,
        log_path=git_repo / ".jri" / "logs" / "ralph" / "stale-detached.log",
        detached=True,
    )

    original_popen = cast(Any, subprocess.Popen)
    expected_command = [sys.executable, "-m", "jri", "-n", "1"]

    def fake_popen(*args: object, **kwargs: object) -> object:
        command = cast(list[str], args[0])
        if command != expected_command:
            return original_popen(*args, **kwargs)
        assert kwargs["cwd"] == git_repo
        assert cast(dict[str, str], kwargs["env"])["JRI_INTERNAL_RUN_LOOP"] == "1"
        assert kwargs["start_new_session"] is True
        return FakeDetachedProcess(313131)

    monkeypatch.setattr("jri.core.service.subprocess.Popen", fake_popen)

    assert service.start(max_tasks=1, detached=True, force=True) == 0
    assert (git_repo / ".jri" / "tasks" / "todo" / "implement-file.md").exists()
    process = cast(
        dict[str, object], read_json(git_repo / ".jri" / "state.json")["process"]
    )
    assert process["loop_pid"] == 313131
    assert process["detached"] is True
    recovery_log = (git_repo / ".jri" / "logs" / "recovery.log").read_text(
        encoding="utf-8"
    )
    assert "mode=detached" in recovery_log
    assert "reason=dead-tracked-process" in recovery_log


def test_ctl_start_detaches_foreground_follow(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0

    popen_calls: list[list[str]] = []
    original_popen = cast(Any, subprocess.Popen)
    expected_command = [
        sys.executable,
        "-m",
        "jri",
        "-n",
        "1",
        "--force",
    ]

    def fake_popen(*args: object, **kwargs: object) -> object:
        command = cast(list[str], args[0])
        if command != expected_command:
            return original_popen(*args, **kwargs)
        popen_calls.append(command)
        assert kwargs["cwd"] == git_repo
        env = cast(dict[str, str], kwargs["env"])
        assert env["JRI_INTERNAL_RUN_LOOP"] == "1"
        assert env["CLICOLOR_FORCE"] == "1"
        assert kwargs["start_new_session"] is True
        return FakeDetachedProcess(515151)

    def fake_follow_log(
        self: JriService,
        log_path: Path,
        *,
        loop_pid: int | None,
        loop_process: object | None = None,
        allow_detach: bool,
    ) -> bool:
        assert re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z-run\.log$",
            log_path.name,
        )
        assert loop_pid == 515151
        assert loop_process is not None
        assert allow_detach is True
        return True

    monkeypatch.setattr("jri.core.service.subprocess.Popen", fake_popen)
    monkeypatch.setattr("jri.core.service.supports_color", lambda: True)
    monkeypatch.setattr(JriService, "_follow_log", fake_follow_log)

    assert run_cli(["start", "-n", "1", "--force"], cwd=git_repo) == 0
    assert popen_calls == [expected_command]
    process = cast(
        dict[str, object], read_json(git_repo / ".jri" / "state.json")["process"]
    )
    assert process["loop_pid"] == 515151
    assert process["detached"] is True


def test_ctl_start_detached_reports_background_run(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    capsys.readouterr()

    original_popen = cast(Any, subprocess.Popen)
    expected_command = [sys.executable, "-m", "jri", "-n", "1"]

    def fake_popen(*args: object, **kwargs: object) -> object:
        command = cast(list[str], args[0])
        if command != expected_command:
            return original_popen(*args, **kwargs)
        assert kwargs["cwd"] == git_repo
        env = cast(dict[str, str], kwargs["env"])
        assert env["JRI_INTERNAL_RUN_LOOP"] == "1"
        assert env["CLICOLOR_FORCE"] == "1"
        assert kwargs["start_new_session"] is True
        return FakeDetachedProcess(616161)

    monkeypatch.setattr("jri.core.service.subprocess.Popen", fake_popen)
    monkeypatch.setattr("jri.core.service.supports_color", lambda: True)

    assert run_cli(["start", "-n", "1", "--detached"], cwd=git_repo) == 0

    output = capsys.readouterr().out
    assert "start: Ralph is running in the background." in output
    assert "jri attach" in output


def test_ctl_start_reports_when_no_todo_tasks(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    capsys.readouterr()

    service = JriService(git_repo)

    def fake_start_attached(**kwargs: object) -> int:
        assert kwargs["force"] is False
        print("No todo tasks found.")
        return 0

    monkeypatch.setattr(service, "start_attached", fake_start_attached)
    monkeypatch.setattr(
        import_module("jri.cli.main"), "JriService", lambda cwd: service
    )

    assert run_cli(["start"], cwd=git_repo) == 0
    assert capsys.readouterr().out == "No todo tasks found.\n"


def test_start_does_not_force_color_without_tty(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    original_popen = cast(Any, subprocess.Popen)
    expected_command = [sys.executable, "-m", "jri", "-n", "1"]

    def fake_popen(*args: object, **kwargs: object) -> object:
        command = cast(list[str], args[0])
        if command != expected_command:
            return original_popen(*args, **kwargs)
        env = cast(dict[str, str], kwargs["env"])
        assert env["JRI_INTERNAL_RUN_LOOP"] == "1"
        assert "CLICOLOR_FORCE" not in env
        return FakeDetachedProcess(717171)

    monkeypatch.setattr("jri.core.service.subprocess.Popen", fake_popen)
    monkeypatch.setattr("jri.core.service.supports_color", lambda: False)

    assert service.start(max_tasks=1, detached=True, force=True) == 0


def test_ctl_start_rejects_managed_ralph_worktree(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0

    worktree_dir = git_repo / ".jri" / "worktree"
    worktree_dir.mkdir(parents=True)
    (worktree_dir / ".jri").mkdir()

    assert run_cli(["start", "-n", "1", "--force"], cwd=worktree_dir) == 1
    assert (
        "start: jri start cannot run from .jri/worktree; "
        "run it from the main repository root" in capsys.readouterr().err
    )


def test_ctl_attach_replays_tracked_run_output(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    log_path = git_repo / ".jri" / "logs" / "ralph" / "attached.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("first line\nsecond line\n", encoding="utf-8")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save_process(
        loop_pid=_dead_pid(),
        child_pid=None,
        log_path=log_path,
        detached=True,
    )

    assert run_cli(["attach"], cwd=git_repo) == 0
    output = capsys.readouterr().out
    assert "first line" in output
    assert "second line" in output


def test_ctl_attach_allows_detach(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    log_path = git_repo / ".jri" / "logs" / "ralph" / "attached.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("first line\n", encoding="utf-8")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save_process(
        loop_pid=_dead_pid(),
        child_pid=None,
        log_path=log_path,
        detached=False,
    )

    def fake_follow_log(
        self: JriService,
        path: Path,
        *,
        loop_pid: int | None,
        loop_process: object | None = None,
        allow_detach: bool,
    ) -> bool:
        assert path == log_path
        assert loop_pid is not None
        assert loop_process is None
        assert allow_detach is True
        return True

    monkeypatch.setattr(JriService, "_follow_log", fake_follow_log)

    assert run_cli(["attach"], cwd=git_repo) == 0
    process = cast(
        dict[str, object], read_json(git_repo / ".jri" / "state.json")["process"]
    )
    assert process["detached"] is True


def test_follow_controls_require_y_then_enter_to_halt() -> None:
    controls = _FollowControls(enabled=True)

    assert controls.handle_key("h") is None
    assert controls.confirming_halt is True
    assert controls.halt_armed is False

    assert controls.handle_key("y") is None
    assert controls.confirming_halt is True
    assert controls.halt_armed is True

    assert controls.handle_key("\n") == "halt"
    assert controls.confirming_halt is False
    assert controls.halt_armed is False


def test_follow_controls_cancel_halt_confirmation() -> None:
    controls = _FollowControls(enabled=True)

    assert controls.handle_key("h") is None
    assert controls.handle_key("n") is None
    assert controls.confirming_halt is False
    assert controls.halt_armed is False


def test_follow_controls_mark_stop_requested_after_s() -> None:
    controls = _FollowControls(enabled=True)

    assert controls.handle_key("s") == "stop"
    assert controls.stop_requested is True
    assert controls.confirming_halt is False
    assert controls.halt_armed is False


def test_follow_controls_cancel_stop_after_second_s() -> None:
    controls = _FollowControls(enabled=True)

    assert controls.handle_key("s") == "stop"
    assert controls.handle_key("s") == "stop_cancel"
    assert controls.stop_requested is False
    assert controls.confirming_halt is False
    assert controls.halt_armed is False


def test_follow_log_stop_control_writes_stop_signal(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    log_path = git_repo / ".jri" / "logs" / "ralph" / "running.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    footer_frames: list[bool] = []

    class FakeControls:
        stop_requested = True
        confirming_halt = False
        halt_armed = False
        _actions = iter(["stop", None])

        def poll_action(self) -> str | None:
            return next(self._actions, None)

    @contextmanager
    def fake_monitor(*, enabled: bool) -> Iterator[FakeControls]:
        assert enabled is True
        yield FakeControls()

    monkeypatch.setattr("jri.core.service.supports_interactive_footer", lambda: True)
    monkeypatch.setattr(service, "_follow_control_monitor", fake_monitor)
    pid_states = iter([True, False])
    monkeypatch.setattr(service, "_is_pid_alive", lambda pid: next(pid_states))
    monkeypatch.setattr(
        "jri.core.service.follow_status_bar",
        lambda *args, **kwargs: (
            footer_frames.append(bool(kwargs["stop_requested"])) or "footer"
        ),
    )
    monkeypatch.setattr("jri.core.service.time.sleep", lambda _: None)

    assert service._follow_log(log_path, loop_pid=12345, allow_detach=True) is False
    assert service.paths.stop_signal_path.exists()
    assert footer_frames == [True]


def test_follow_log_detach_notice_is_cyan_when_color_is_enabled(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    log_path = git_repo / ".jri" / "logs" / "ralph" / "running.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")

    class FakeControls:
        stop_requested = False
        confirming_halt = False
        halt_armed = False
        _actions = iter(["detach"])

        def poll_action(self) -> str | None:
            return next(self._actions, None)

    @contextmanager
    def fake_monitor(*, enabled: bool) -> Iterator[FakeControls]:
        assert enabled is True
        yield FakeControls()

    monkeypatch.setattr("jri.core.service.supports_interactive_footer", lambda: True)
    monkeypatch.setattr("jri.core.service.supports_color", lambda: True)
    monkeypatch.setattr("jri.core.ui.supports_color", lambda: True)
    monkeypatch.setattr(service, "_follow_control_monitor", fake_monitor)

    assert service._follow_log(log_path, loop_pid=12345, allow_detach=True) is True
    assert capsys.readouterr().out == (
        f"{CYAN}Detached. Use `jri attach` to follow the run again.{RESET}\n"
    )


def test_follow_log_shows_saved_stop_request_after_attach(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    log_path = git_repo / ".jri" / "logs" / "ralph" / "running.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    service.paths.stop_signal_path.parent.mkdir(parents=True, exist_ok=True)
    service.paths.stop_signal_path.write_text("requested\n", encoding="utf-8")
    footer_frames: list[bool] = []

    class FakeControls:
        confirming_halt = False
        halt_armed = False

        def poll_action(self) -> str | None:
            return None

    @contextmanager
    def fake_monitor(*, enabled: bool) -> Iterator[FakeControls]:
        assert enabled is True
        yield FakeControls()

    monkeypatch.setattr("jri.core.service.supports_interactive_footer", lambda: True)
    monkeypatch.setattr(service, "_follow_control_monitor", fake_monitor)
    pid_states = iter([True, False])
    monkeypatch.setattr(service, "_is_pid_alive", lambda pid: next(pid_states))
    monkeypatch.setattr(
        "jri.core.service.follow_status_bar",
        lambda *args, **kwargs: (
            footer_frames.append(bool(kwargs["stop_requested"])) or "footer"
        ),
    )
    monkeypatch.setattr("jri.core.service.time.sleep", lambda _: None)

    assert service._follow_log(log_path, loop_pid=12345, allow_detach=True) is False
    assert footer_frames == [True]


def test_follow_log_halt_control_invokes_halt(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    log_path = git_repo / ".jri" / "logs" / "ralph" / "running.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("running\n", encoding="utf-8")
    halt_calls: list[str] = []

    class FakeControls:
        stop_requested = False
        confirming_halt = False
        halt_armed = True
        _actions = iter(["halt"])

        def poll_action(self) -> str | None:
            return next(self._actions, None)

    @contextmanager
    def fake_monitor(*, enabled: bool) -> Iterator[FakeControls]:
        assert enabled is True
        yield FakeControls()

    monkeypatch.setattr("jri.core.service.supports_interactive_footer", lambda: True)
    monkeypatch.setattr(service, "_follow_control_monitor", fake_monitor)
    monkeypatch.setattr(service, "halt", lambda: halt_calls.append("halt"))

    assert service._follow_log(log_path, loop_pid=12345, allow_detach=True) is False
    assert halt_calls == ["halt"]


def test_follow_log_shows_spinner_for_running_subagent(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    log_path = git_repo / ".jri" / "logs" / "ralph" / "running.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "type": "tool",
                        "id": "tool-1",
                        "tool": "task",
                        "state": {
                            "status": "running",
                            "input": {"description": "research phase"},
                        },
                    }
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message.part.delta",
                "properties": {
                    "field": "text",
                    "delta": "Spawned implementation subagent",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    footer_calls: list[tuple[str | None, str | None]] = []

    class FakeControls:
        stop_requested = False
        confirming_halt = False
        halt_armed = False

        def poll_action(self) -> str | None:
            return None

    @contextmanager
    def fake_monitor(*, enabled: bool) -> Iterator[FakeControls]:
        assert enabled is True
        yield FakeControls()

    monkeypatch.setattr("jri.core.service.supports_interactive_footer", lambda: True)
    monkeypatch.setattr(service, "_follow_control_monitor", fake_monitor)
    pid_states = iter([True, False])
    monkeypatch.setattr(service, "_is_pid_alive", lambda pid: next(pid_states))
    monkeypatch.setattr(
        "jri.core.service.follow_status_bar",
        lambda *args, **kwargs: (
            footer_calls.append(
                cast(
                    tuple[str | None, str | None],
                    (kwargs.get("activity"), kwargs.get("spinner_frame")),
                )
            )
            or "footer"
        ),
    )
    monkeypatch.setattr("jri.core.service.time.sleep", lambda _: None)

    assert service._follow_log(log_path, loop_pid=12345, allow_detach=True) is False
    assert len(footer_calls) == 1
    assert footer_calls[0][0] == "research phase"
    spinner_frame = footer_calls[0][1]
    assert spinner_frame is not None
    assert spinner_frame in "|/-\\"


def test_follow_log_redraws_footer_across_repeated_resizes(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    log_path = git_repo / ".jri" / "logs" / "ralph" / "running.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")

    class FakeControls:
        stop_requested = False
        confirming_halt = False
        halt_armed = False

        def poll_action(self) -> str | None:
            return None

    @contextmanager
    def fake_monitor(*, enabled: bool) -> Iterator[FakeControls]:
        assert enabled is True
        yield FakeControls()

    terminal_sizes = iter(
        [
            os.terminal_size((60, 20)),
            os.terminal_size((60, 10)),
            os.terminal_size((60, 20)),
            os.terminal_size((60, 10)),
        ]
    )

    pid_states = iter([True, True, True, True, False])

    monkeypatch.setattr("jri.core.service.supports_interactive_footer", lambda: True)
    monkeypatch.setattr(service, "_follow_control_monitor", fake_monitor)
    monkeypatch.setattr(service, "_current_follow_task", lambda: "task-a")
    monkeypatch.setattr(service, "_is_pid_alive", lambda pid: next(pid_states))
    monkeypatch.setattr(
        "jri.core.service.shutil.get_terminal_size",
        lambda _: next(terminal_sizes),
    )
    monkeypatch.setattr("jri.core.service.time.sleep", lambda _: None)

    assert service._follow_log(log_path, loop_pid=12345, allow_detach=True) is False

    output = capsys.readouterr().out
    assert output.count("\0337\033[20;1H\033[2K") == 4
    assert output.count("\0337\033[10;1H\033[2K") == 4


def test_follow_log_stops_when_spawned_process_has_exited(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    log_path = git_repo / ".jri" / "logs" / "ralph" / "completed.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("completed\n", encoding="utf-8")

    class ZombieLikeProcess:
        def poll(self) -> int:
            return 0

    monkeypatch.setattr(service, "_is_pid_alive", lambda pid: True)

    detached = service._follow_log(
        log_path,
        loop_pid=12345,
        loop_process=cast(Any, ZombieLikeProcess()),
        allow_detach=False,
    )

    assert detached is False
    assert capsys.readouterr().out == "completed\n"


def test_follow_log_renders_saved_events_instead_of_raw_json(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    log_path = git_repo / ".jri" / "logs" / "ralph" / "completed.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "message.part.updated",
                        "properties": {
                            "part": {
                                "type": "tool",
                                "id": "tool-1",
                                "tool": "task",
                                "state": {
                                    "status": "running",
                                    "input": {"description": "research phase"},
                                },
                            }
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "message.part.delta",
                        "properties": {
                            "field": "text",
                            "delta": "Spawned implementation subagent",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    detached = service._follow_log(log_path, loop_pid=None, allow_detach=False)

    assert detached is False
    output = capsys.readouterr().out
    assert "⚙ task research phase" in output
    assert "Spawned implementation subagent" in output
    assert '"type": "message.part.updated"' not in output


def test_view_inspect_pretty_prints_saved_task_log(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    log_path = git_repo / ".jri" / "logs" / "ralph" / "task-a.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "message.part.updated",
                        "properties": {
                            "part": {
                                "type": "tool",
                                "id": "tool-1",
                                "tool": "read",
                                "state": {
                                    "status": "running",
                                    "input": {"filePath": ".jri/tasks/doing/task-a.md"},
                                },
                            }
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "message.part.delta",
                        "properties": {"field": "text", "delta": "Applying fix"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    attempt = AttemptState(
        number=1,
        task_slug="task-a",
        branch="ralph",
        started_at=1,
        finished_at=2,
        log_path=str(log_path),
        result="completed",
    )
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save(State(attempts=[attempt]))

    assert run_cli(["inspect", "task-a"], cwd=git_repo) == 0
    output = capsys.readouterr().out
    assert "task-a" in output
    assert "⚙ read .jri/tasks/doing/task-a.md" in output
    assert "Applying fix" in output
    assert "completed" in output


def test_view_inspect_defaults_to_active_attempt(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    log_path = git_repo / ".jri" / "logs" / "ralph" / "current.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("still running\n", encoding="utf-8")

    attempt = AttemptState(
        number=1,
        task_slug="current-task",
        branch="ralph",
        started_at=1,
        log_path=str(log_path),
    )
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save(State(active_attempt=attempt, attempts=[attempt]))

    assert run_cli(["inspect"], cwd=git_repo) == 0
    output = capsys.readouterr().out
    assert "current-task" in output
    assert "still running" in output


def test_view_inspect_reads_historical_attempt_when_runtime_state_is_missing(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    log_path = git_repo / ".jri" / "logs" / "ralph" / "task-a-history.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "message.part.updated",
                        "properties": {
                            "part": {
                                "type": "tool",
                                "id": "tool-1",
                                "tool": "read",
                                "state": {
                                    "status": "running",
                                    "input": {"filePath": ".jri/tasks/done/task-a.md"},
                                },
                            }
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "message.part.delta",
                        "properties": {"field": "text", "delta": "Replaying history"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    history_path = git_repo / ".jri" / "attempts" / "task-a.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps(
            {
                "task_slug": "task-a",
                "attempts": [
                    {
                        "number": 1,
                        "task_slug": "task-a",
                        "branch": "ralph",
                        "started_at": 1,
                        "finished_at": 2,
                        "log_path": str(
                            git_repo / ".jri" / "logs" / "ralph" / "missing.log"
                        ),
                        "result": "failed",
                    },
                    {
                        "number": 2,
                        "task_slug": "task-a",
                        "branch": "ralph",
                        "started_at": 3,
                        "finished_at": 4,
                        "log_path": str(log_path),
                        "result": "completed",
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert run_cli(["inspect", "task-a"], cwd=git_repo) == 0
    output = capsys.readouterr().out
    assert "task-a" in output
    assert "⚙ read .jri/tasks/done/task-a.md" in output
    assert "Replaying history" in output
    assert "completed" in output


def test_start_retries_after_interrupted_completion_without_rerunning_task(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="task-a",
        title="Task A",
        priority=0,
        assignee="Ralph",
        body="Complete task A.",
    )
    write_task(
        git_repo,
        status="todo",
        slug="task-b",
        title="Task B",
        priority=1,
        assignee="Ralph",
        body="Complete task B.",
    )
    git(git_repo, "add", ".jri/tasks/todo")
    git(git_repo, "commit", "-m", "add retry tasks")

    first_client = SuccessfulFakeAgentRuntime()
    first_service = JriService(git_repo, agent_runtime=first_client)

    def interrupted_mark_task_finished(*, task_slug: str, finished_at: int) -> None:
        raise KeyboardInterrupt("simulated interruption during completion")

    monkeypatch.setattr(
        first_service.state_store,
        "mark_task_finished",
        interrupted_mark_task_finished,
    )

    with pytest.raises(KeyboardInterrupt, match="simulated interruption"):
        first_service.start(max_tasks=1, force=True)

    interrupted_state = read_json(git_repo / ".jri" / "state.json")
    interrupted_attempt = cast(dict[str, object], interrupted_state["active_attempt"])
    assert interrupted_attempt["task_slug"] == "task-a"
    assert interrupted_attempt["result"] == "completed"
    assert (git_repo / ".jri" / "tasks" / "done" / "task-a.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "task-a.md").exists()
    assert (git_repo / ".jri" / "tasks" / "todo" / "task-b.md").exists()
    assert git(git_repo, "branch", "--show-current") == "main"
    assert "jri/end/task-a" in git(git_repo, "tag").splitlines()

    retry_client = SuccessfulFakeAgentRuntime()
    retry_service = JriService(git_repo, agent_runtime=retry_client)

    assert retry_service.start(max_tasks=1, force=True) == 1
    assert len(retry_client.calls) == 1
    assert "task-b" in retry_client.calls[0][0]

    final_state = read_json(git_repo / ".jri" / "state.json")
    attempts = cast(list[dict[str, object]], final_state["attempts"])
    assert final_state.get("active_attempt") is None
    assert [attempt["task_slug"] for attempt in attempts] == ["task-a", "task-b"]
    assert attempts[0]["result"] == "completed"
    assert attempts[1]["result"] == "completed"
    assert (git_repo / ".jri" / "tasks" / "done" / "task-b.md").exists()


def test_stop_creates_stop_signal(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    service.stop("maintenance window")

    assert (git_repo / ".jri" / "signals" / "stop").read_text(
        encoding="utf-8"
    ) == "maintenance window\n"


def test_ctl_stop_reports_stop_request(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    capsys.readouterr()

    assert run_cli(["stop", "maintenance"], cwd=git_repo) == 0

    output = capsys.readouterr().out
    assert "stop: stop requested; Ralph will stop after the current task." in output
    assert (git_repo / ".jri" / "signals" / "stop").read_text(
        encoding="utf-8"
    ) == "maintenance\n"


def test_ctl_stop_cancel_removes_stop_signal(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.stop("maintenance")
    capsys.readouterr()

    assert run_cli(["stop", "--cancel"], cwd=git_repo) == 0

    output = capsys.readouterr().out
    assert "stop_cancel: stop request canceled." in output
    assert not (git_repo / ".jri" / "signals" / "stop").exists()


def test_reset_returns_repo_to_last_successful_task(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    assert service.start(max_tasks=1, force=True) == 1
    service.state_store.save_session("ses_interrogation")

    (git_repo / "extra.txt").write_text("later\n", encoding="utf-8")
    git(git_repo, "add", "extra.txt")
    git(git_repo, "commit", "-m", "extra")

    service.reset()

    assert not (git_repo / "extra.txt").exists()
    state = read_json(git_repo / ".jri" / "state.json")
    assert state["session"] == "ses_interrogation"
    assert "finished_at" in state


def test_halt_terminates_tracked_process(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    sleeper = subprocess.Popen(["sleep", "30"])
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save_process(
        loop_pid=sleeper.pid, child_pid=None, log_path=None, detached=True
    )

    try:
        service.halt()
        sleeper.wait(timeout=5)
    finally:
        if sleeper.poll() is None:
            os.kill(sleeper.pid, signal.SIGTERM)

    assert sleeper.returncode is not None


def test_ctl_halt_reports_success(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    capsys.readouterr()
    sleeper = subprocess.Popen(["sleep", "30"])
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save_process(
        loop_pid=sleeper.pid, child_pid=None, log_path=None, detached=True
    )

    try:
        assert run_cli(["halt"], cwd=git_repo) == 0
        sleeper.wait(timeout=5)
    finally:
        if sleeper.poll() is None:
            os.kill(sleeper.pid, signal.SIGTERM)

    assert "halt: tracked Ralph process stopped." in capsys.readouterr().out


def test_halt_skips_current_process_and_terminates_tracked_child(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save_process(
        loop_pid=os.getpid(),
        child_pid=424242,
        log_path=None,
        detached=False,
    )

    kill_calls: list[int] = []
    killpg_calls: list[int] = []

    monkeypatch.setattr("jri.core.service.os.getpgrp", lambda: 999)
    monkeypatch.setattr(
        "jri.core.service.os.getpgid",
        lambda pid: 999 if pid == 424242 else 0,
    )
    monkeypatch.setattr(
        "jri.core.service.os.kill",
        lambda pid, sig: kill_calls.append(pid),
    )
    monkeypatch.setattr(
        "jri.core.service.os.killpg", lambda pgid, sig: killpg_calls.append(pgid)
    )

    service.halt()

    assert kill_calls == [424242]
    assert killpg_calls == []
    state = read_json(git_repo / ".jri" / "state.json")
    assert state.get("process") is None


def test_needs_human_generates_human_followup_and_blocks_original_task(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="needs-human-task",
        title="Needs human task",
        priority=0,
        assignee="Ralph",
        body="This will need human help.",
    )
    git(git_repo, "add", ".jri/tasks/todo/needs-human-task.md")
    git(git_repo, "commit", "-m", "add needs human task")

    service = JriService(git_repo, agent_runtime=NeedsHumanFakeAgentRuntime())

    completed = service.start(max_tasks=1, force=True)

    todo_tasks = list_tasks(git_repo / ".jri" / "tasks" / "todo")
    original_task = parse_task_file(
        git_repo / ".jri" / "tasks" / "todo" / "needs-human-task.md"
    )
    human_tasks = [task for task in todo_tasks if task.metadata.assignee == "Human"]

    assert completed == 0
    assert len(human_tasks) == 1
    human_task = human_tasks[0]
    assert human_task.slug == "needs-human-task--needs-human"
    assert human_task.metadata.title == "Provide missing input"
    assert human_task.metadata.priority == 0
    assert human_task.metadata.acceptance_criteria == ["Required input is provided"]
    assert original_task.metadata.depends_on == [human_task.slug]
    assert "## Blocker" in human_task.body
    assert "A human action is required." in human_task.body
    assert "## Requested human work" in human_task.body
    assert "A human must provide the missing input." in human_task.body
    assert "needs-human-task" in human_task.body
    assert ".jri/tasks/todo/needs-human-task.md" in human_task.body
    assert ".jri/logs/ralph/" in human_task.body
    assert "ses_needs_human" in human_task.body
    assert ".jri/logs/external/pi/ses_needs_human.json" in human_task.body
    assert (
        git_repo / ".jri" / "logs" / "external" / "pi" / "ses_needs_human.json"
    ).exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "needs-human-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "done" / "needs-human-task.md").exists()
    state = read_json(git_repo / ".jri" / "state.json")
    attempts = cast(list[dict[str, object]], state["attempts"])
    assert attempts[0]["result"] == "needs_human"
    assert attempts[0]["result_payload"] == {
        "result": "needs_human",
        "blocker": "A human action is required.",
        "human_task": {
            "title": "Provide missing input",
            "body": "A human must provide the missing input.",
            "acceptance_criteria": ["Required input is provided"],
        },
    }
    attempt_history = read_json(
        git_repo / ".jri" / "attempts" / "needs-human-task.json"
    )
    history_attempts = cast(list[dict[str, object]], attempt_history["attempts"])
    assert history_attempts[0]["result_payload"] == attempts[0]["result_payload"]

    timeline = [
        json.loads(line)
        for line in (git_repo / ".jri" / "logs" / "timeline.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    escalated = [event for event in timeline if event["event"] == "task_escalated"]
    assert escalated == [
        {
            "ts": escalated[0]["ts"],
            "event": "task_escalated",
            "task": "needs-human-task",
            "detail": {
                "attempt": 1,
                "blocker": "A human action is required.",
                "human_task": human_tasks[0].slug,
                "session_id": "ses_needs_human",
            },
        }
    ]
    assert git(git_repo, "branch", "--show-current") == "main"
    tags = git(git_repo, "tag").splitlines()
    assert "jri/1" not in tags
    # The persistent Ralph worktree branch remains available.
    branches = git(git_repo, "branch", "--format=%(refname:short)").splitlines()
    assert "ralph/main" in branches
    assert "ralph" not in branches


def test_needs_human_block_is_durable_across_runs(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="needs-human-task",
        title="Needs human task",
        priority=0,
        assignee="Ralph",
        body="This will need human help.",
    )
    git(git_repo, "add", ".jri/tasks/todo/needs-human-task.md")
    git(git_repo, "commit", "-m", "add needs human task")

    service = JriService(git_repo, agent_runtime=NeedsHumanFakeAgentRuntime())

    assert service.start(max_tasks=1, force=True) == 0

    retry_client = SuccessfulFakeAgentRuntime()
    retry_service = JriService(git_repo, agent_runtime=retry_client)

    assert retry_service.start(max_tasks=1, force=True) == 0
    assert retry_client.calls == []


def test_needs_human_then_successful_completes_one(git_repo: Path) -> None:
    """Two tasks: first needs human, loop continues and completes the second."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="task-a",
        title="Task A (needs human)",
        priority=0,
        assignee="Ralph",
        body="Will need human help.",
    )
    write_task(
        git_repo,
        status="todo",
        slug="task-b",
        title="Task B (success)",
        priority=1,
        assignee="Ralph",
        body="Will succeed.",
    )
    git(git_repo, "add", ".jri/tasks/todo/")
    git(git_repo, "commit", "-m", "add two tasks")

    client = NeedsHumanThenSuccessfulFakeAgentRuntime()
    service = JriService(git_repo, agent_runtime=client)

    completed = service.start(max_tasks=2, force=True)

    todo_tasks = list_tasks(git_repo / ".jri" / "tasks" / "todo")
    task_a = parse_task_file(git_repo / ".jri" / "tasks" / "todo" / "task-a.md")
    human_tasks = [task for task in todo_tasks if task.metadata.assignee == "Human"]

    assert completed == 1
    assert len(human_tasks) == 1
    assert task_a.metadata.depends_on == [human_tasks[0].slug]
    # Needs-human task is back in todo, now blocked on a generated Human task
    assert (git_repo / ".jri" / "tasks" / "todo" / "task-a.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "done" / "task-a.md").exists()
    # Successful task is in done
    assert (git_repo / ".jri" / "tasks" / "done" / "task-b.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "todo" / "task-b.md").exists()
    # Only the successful branch remains
    branches = git(git_repo, "branch", "--format=%(refname:short)").splitlines()
    assert not any("task-a" in b for b in branches)
    assert git(git_repo, "branch", "--show-current") == "main"


class MakeCheckFailsFakeAgentRuntime(FakeAgentRuntime):
    """Successful Ralph run, but the project has a failing make check."""

    def __init__(self) -> None:
        super().__init__(model=None)
        self.calls: list[tuple[str, Path]] = []

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        self.calls.append((prompt, log_path))
        (root / "implemented.txt").write_text("implemented\n", encoding="utf-8")
        log_path.write_text("fake run\n", encoding="utf-8")
        return AgentRunResult(returncode=0, session_id="ses_fake", result="completed")

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text('{"session": "fake"}\n', encoding="utf-8")


class FailedFakeAgentRuntime(FakeAgentRuntime):
    """Simulates a runtime-level failed run.

    Ralph does not produce a valid result.
    """

    def __init__(self) -> None:
        super().__init__(model=None)
        self.calls: list[tuple[str, Path]] = []

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        self.calls.append((prompt, log_path))
        log_path.write_text("fake failed run\n", encoding="utf-8")
        return AgentRunResult(returncode=0, session_id="ses_failed", result="failed")

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text('{"session": "fake_failed"}\n', encoding="utf-8")


class IncompleteFakeAgentRuntime(FakeAgentRuntime):
    """Simulates Ralph returning an incompleted result."""

    def __init__(self) -> None:
        super().__init__(model=None)
        self.calls: list[tuple[str, Path]] = []

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        self.calls.append((prompt, log_path))
        log_path.write_text("fake incompleted run\n", encoding="utf-8")
        return AgentRunResult(
            returncode=0,
            session_id="ses_incomplete",
            result="incompleted",
            payload=RalphResultPayload(
                result="incompleted",
                summary="The task needs another pass.",
                learnings=["A retry should resume from the partial state."],
            ),
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text('{"session": "fake_incomplete"}\n', encoding="utf-8")


class IncompleteWithoutLearningsFakeAgentRuntime(FakeAgentRuntime):
    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        del root, prompt, result_path, on_start, timeout
        log_path.write_text("fake incompleted run\n", encoding="utf-8")
        return AgentRunResult(
            returncode=0,
            session_id="ses_incomplete",
            result="incompleted",
            payload=RalphResultPayload(
                result="incompleted",
                summary="The task needs another pass.",
            ),
        )


class LearningSensitiveFakeAgentRuntime(FakeAgentRuntime):
    def __init__(self) -> None:
        super().__init__(model=None)
        self.calls: list[str] = []

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        del result_path, on_start, timeout
        self.calls.append(prompt)
        log_path.write_text("fake learning-sensitive run\n", encoding="utf-8")
        if "Use the existing helper." not in prompt:
            return AgentRunResult(
                returncode=0,
                session_id="ses_incomplete",
                result="incompleted",
                payload=RalphResultPayload(
                    result="incompleted",
                    summary="The helper was missed.",
                    learnings=["Use the existing helper."],
                ),
            )
        (root / "implemented.txt").write_text("implemented\n", encoding="utf-8")
        return AgentRunResult(
            returncode=0,
            session_id="ses_ok",
            result="completed",
            payload=RalphResultPayload(result="completed", summary="Done."),
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text(f'{{"session": "{session_id}"}}\n', encoding="utf-8")


class NonzeroFakeAgentRuntime(FakeAgentRuntime):
    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        del root, prompt, result_path, on_start, timeout
        log_path.write_text("fake nonzero run\n", encoding="utf-8")
        return AgentRunResult(returncode=7, session_id="ses_nonzero", result="failed")


class MalformedNeedsHumanFakeAgentRuntime(FakeAgentRuntime):
    """Simulates a malformed structured needs_human payload from Ralph."""

    def __init__(self) -> None:
        super().__init__(model=None)

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        log_path.write_text("fake malformed needs-human run\n", encoding="utf-8")
        return AgentRunResult(
            returncode=0,
            session_id="ses_bad_needs_human",
            result="failed",
            warnings=[
                "invalid result payload; treating run as failed: "
                "`human_task.title` must be a non-empty string"
            ],
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text(
            '{"session": "fake_bad_needs_human"}\n', encoding="utf-8"
        )


def test_failed_outcome_triggers_recovery(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="failing-task",
        title="Failing task",
        priority=0,
        assignee="Ralph",
        body="This will fail.",
    )
    git(git_repo, "add", ".jri/tasks/todo/failing-task.md")
    git(git_repo, "commit", "-m", "add failing task")

    service = JriService(git_repo, agent_runtime=FailedFakeAgentRuntime())

    completed = service.start(max_tasks=1, force=True)

    assert completed == 0
    assert (git_repo / ".jri" / "tasks" / "todo" / "failing-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "failing-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "done" / "failing-task.md").exists()
    assert git(git_repo, "branch", "--show-current") == "main"
    branches = git(git_repo, "branch", "--format=%(refname:short)").splitlines()
    assert "ralph/main" in branches
    assert "ralph" not in branches


def test_start_summary_distinguishes_task_failure(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="failing-task",
        title="Failing task",
        priority=0,
        assignee="Ralph",
        body="This will fail.",
    )
    git(git_repo, "add", ".jri/tasks/todo/failing-task.md")
    git(git_repo, "commit", "-m", "add failing task")

    service = JriService(git_repo, agent_runtime=FailedFakeAgentRuntime())

    summary = service.start_summary(max_tasks=1, force=True)

    assert summary.completed == 0
    assert summary.outcome == "task_failure"
    assert summary.task_results == {"failing-task": "failed"}


def test_start_summary_distinguishes_no_work(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    summary = service.start_summary(max_tasks=1, force=True)

    assert summary.completed == 0
    assert summary.outcome == "no_work"
    assert summary.task_results == {}


def test_incomplete_result_triggers_retryable_recovery(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="incomplete-task",
        title="Incomplete task",
        priority=0,
        assignee="Ralph",
        body="This will be left incomplete.",
    )
    git(git_repo, "add", ".jri/tasks/todo/incomplete-task.md")
    git(git_repo, "commit", "-m", "add incomplete task")

    service = JriService(git_repo, agent_runtime=IncompleteFakeAgentRuntime())

    completed = service.start(max_tasks=1, force=True)

    assert completed == 0
    assert (git_repo / ".jri" / "tasks" / "todo" / "incomplete-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "incomplete-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "done" / "incomplete-task.md").exists()
    state = read_json(git_repo / ".jri" / "state.json")
    attempts = cast(list[dict[str, object]], state["attempts"])
    assert attempts[0]["result"] == "incompleted"
    assert attempts[0]["result_payload"] == {
        "result": "incompleted",
        "summary": "The task needs another pass.",
        "learnings": ["A retry should resume from the partial state."],
    }
    assert [
        t
        for t in list_tasks(git_repo / ".jri" / "tasks" / "todo")
        if t.metadata.assignee == "Human"
    ] == []


def test_incomplete_result_requires_learnings(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="incomplete-task",
        title="Incomplete task",
        priority=0,
        assignee="Ralph",
        body="This will be left incomplete.",
    )
    git(git_repo, "add", ".jri/tasks/todo/incomplete-task.md")
    git(git_repo, "commit", "-m", "add incomplete task")

    service = JriService(
        git_repo,
        agent_runtime=IncompleteWithoutLearningsFakeAgentRuntime(),
    )

    completed = service.start(max_tasks=1, force=True)

    assert completed == 0
    attempts = cast(
        list[dict[str, object]],
        read_json(git_repo / ".jri" / "state.json")["attempts"],
    )
    assert attempts[0]["result"] == "failed"


def test_retry_prompt_includes_previous_attempt_learnings(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="retry-task",
        title="Retry task",
        priority=0,
        assignee="Ralph",
        body="This task needs the retry learning.",
    )
    git(git_repo, "add", ".jri/tasks/todo/retry-task.md")
    git(git_repo, "commit", "-m", "add retry task")

    runtime = LearningSensitiveFakeAgentRuntime()
    first = JriService(git_repo, agent_runtime=runtime)
    assert first.start(max_tasks=1, force=True) == 0

    second = JriService(git_repo, agent_runtime=runtime)
    assert second.start(max_tasks=1, force=True) == 1

    assert len(runtime.calls) == 2
    assert "Previous attempts" not in runtime.calls[0]
    assert "Previous attempts" in runtime.calls[1]
    assert "Result: incompleted" in runtime.calls[1]
    assert "Summary: The helper was missed." in runtime.calls[1]
    assert "- Use the existing helper." in runtime.calls[1]


def test_nonzero_agent_return_records_failed_attempt_without_crashing(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="nonzero-task",
        title="Nonzero task",
        priority=0,
        assignee="Ralph",
        body="This returns a nonzero runtime code.",
    )
    git(git_repo, "add", ".jri/tasks/todo/nonzero-task.md")
    git(git_repo, "commit", "-m", "add nonzero task")

    service = JriService(git_repo, agent_runtime=NonzeroFakeAgentRuntime())

    assert service.start(max_tasks=1, force=True) == 0
    attempts = cast(
        list[dict[str, object]],
        read_json(git_repo / ".jri" / "state.json")["attempts"],
    )
    assert attempts[0]["result"] == "failed"
    timeline = (git_repo / ".jri" / "logs" / "timeline.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"reason":"nonzero_returncode"' in timeline


def test_malformed_needs_human_payload_is_treated_as_failed(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="bad-needs-human-task",
        title="Bad needs human task",
        priority=0,
        assignee="Ralph",
        body="This returns a malformed needs_human payload.",
    )
    git(git_repo, "add", ".jri/tasks/todo/bad-needs-human-task.md")
    git(git_repo, "commit", "-m", "add malformed needs human task")

    service = JriService(
        git_repo,
        agent_runtime=MalformedNeedsHumanFakeAgentRuntime(),
    )

    completed = service.start(max_tasks=1, force=True)

    assert completed == 0
    assert (git_repo / ".jri" / "tasks" / "todo" / "bad-needs-human-task.md").exists()
    todo_tasks = list_tasks(git_repo / ".jri" / "tasks" / "todo")
    assert [task for task in todo_tasks if task.metadata.assignee == "Human"] == []
    state = read_json(git_repo / ".jri" / "state.json")
    attempts = cast(list[dict[str, object]], state["attempts"])
    assert attempts[0]["result"] == "failed"


def test_make_check_runs_after_completion(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    # Create a Makefile with a passing check target
    (git_repo / "Makefile").write_text("check:\n\t@echo ok\n", encoding="utf-8")
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "add", "Makefile")
    git(git_repo, "commit", "-m", "add task and makefile")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    completed = service.start(max_tasks=1, force=True)

    assert completed == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "implement-file.md").exists()
    assert git(git_repo, "branch", "--show-current") == "main"


def test_make_check_pass_records_metric(git_repo: Path) -> None:
    """A passing make check records a pass metric entry."""
    import json

    assert run_cli(["init"], cwd=git_repo) == 0
    (git_repo / "Makefile").write_text("check:\n\t@echo ok\n", encoding="utf-8")
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "add", "Makefile")
    git(git_repo, "commit", "-m", "add task and makefile")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    completed = service.start(max_tasks=1, force=True)

    assert completed == 1
    metrics_path = git_repo / ".jri" / "metrics.json"
    assert metrics_path.exists()
    entries = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert len(entries) == 1
    assert entries[0]["result"] == "pass"
    assert entries[0]["task"] == "implement-file"


def test_failing_make_check_records_metric(git_repo: Path) -> None:
    """A failing make check records a fail metric entry."""
    import json

    assert run_cli(["init"], cwd=git_repo) == 0
    (git_repo / "Makefile").write_text("check:\n\texit 1\n", encoding="utf-8")
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "add", "Makefile")
    git(git_repo, "commit", "-m", "add task and makefile")

    service = JriService(git_repo, agent_runtime=MakeCheckFailsFakeAgentRuntime())
    completed = service.start(max_tasks=1, force=True)

    assert completed == 0
    metrics_path = git_repo / ".jri" / "metrics.json"
    assert metrics_path.exists()
    entries = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert len(entries) == 1
    assert entries[0]["result"] == "fail"
    assert entries[0]["task"] == "implement-file"


def test_failing_make_check_triggers_recovery(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    # Create a Makefile with a failing check target
    (git_repo / "Makefile").write_text("check:\n\texit 1\n", encoding="utf-8")
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "add", "Makefile")
    git(git_repo, "commit", "-m", "add task and makefile")

    service = JriService(git_repo, agent_runtime=MakeCheckFailsFakeAgentRuntime())

    completed = service.start(max_tasks=1, force=True)

    assert completed == 0
    # Task should be back in todo after recovery
    assert (git_repo / ".jri" / "tasks" / "todo" / "implement-file.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "implement-file.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "done" / "implement-file.md").exists()
    assert git(git_repo, "branch", "--show-current") == "main"
    # The persistent Ralph worktree branch remains available.
    branches = git(git_repo, "branch", "--format=%(refname:short)").splitlines()
    assert "ralph/main" in branches
    assert "ralph" not in branches


def test_missing_make_binary_records_failure_and_recovers(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import jri.core.service as service_module
    from jri.core.timeline import TimelineStore

    assert run_cli(["init"], cwd=git_repo) == 0
    (git_repo / "Makefile").write_text("check:\n\t@true\n", encoding="utf-8")
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md", "Makefile")
    git(git_repo, "commit", "-m", "add task and makefile")

    real_run = service_module.subprocess.run

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args and args[0] == ["make", "check"]:
            raise FileNotFoundError("make")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(service_module.subprocess, "run", fake_run)

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    completed = service.start(max_tasks=1, force=True)

    assert completed == 0
    assert "make: command not found" in capsys.readouterr().err
    assert (git_repo / ".jri" / "tasks" / "todo" / "implement-file.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "done" / "implement-file.md").exists()
    metrics = json.loads(
        (git_repo / ".jri" / "metrics.json").read_text(encoding="utf-8")
    )
    assert metrics == [
        {"task": "implement-file", "ts": metrics[0]["ts"], "result": "fail"}
    ]
    events = TimelineStore(git_repo / ".jri" / "logs" / "timeline.jsonl").read()
    warning_events = [event for event in events if event.event == "stderr_warning"]
    assert warning_events[-1].task == "implement-file"
    assert warning_events[-1].detail == {"message": "make: command not found"}


def test_failed_task_is_retried_after_first_failure(git_repo: Path) -> None:
    """A task that fails once is retried on the next loop invocation."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="failing-task",
        title="Failing task",
        priority=0,
        assignee="Ralph",
        body="This will fail once.",
    )
    git(git_repo, "add", ".jri/tasks/todo/failing-task.md")
    git(git_repo, "commit", "-m", "add failing task")

    # First run fails
    fail_client = FailedFakeAgentRuntime()
    fail_service = JriService(git_repo, agent_runtime=fail_client)
    completed = fail_service.start(max_tasks=1, force=True)

    assert completed == 0
    assert (git_repo / ".jri" / "tasks" / "todo" / "failing-task.md").exists()
    # One failed attempt recorded
    state = read_json(git_repo / ".jri" / "state.json")
    attempts = cast(list[dict[str, object]], state["attempts"])
    failed_for_task = [a for a in attempts if a["task_slug"] == "failing-task"]
    assert len(failed_for_task) == 1
    assert failed_for_task[0]["result"] == "failed"
    history = read_json(git_repo / ".jri" / "attempts" / "failing-task.json")
    assert len(cast(list[dict[str, object]], history["attempts"])) == 1

    # Second run succeeds (task is retried)
    success_client = SuccessfulFakeAgentRuntime()
    success_service = JriService(git_repo, agent_runtime=success_client)
    completed = success_service.start(max_tasks=1, force=True)

    assert completed == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "failing-task.md").exists()
    assert len(success_client.calls) == 1
    assert "failing-task" in success_client.calls[0][0]


def test_failed_task_is_retried_up_to_three_times(git_repo: Path) -> None:
    """A task that fails twice is still retryable on the next start."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="failing-task",
        title="Failing task",
        priority=0,
        assignee="Ralph",
        body="This will fail twice.",
    )
    git(git_repo, "add", ".jri/tasks/todo/failing-task.md")
    git(git_repo, "commit", "-m", "add failing task")

    # First run fails
    service1 = JriService(git_repo, agent_runtime=FailedFakeAgentRuntime())
    assert service1.start(max_tasks=1, force=True) == 0

    # Second run also fails
    service2 = JriService(git_repo, agent_runtime=FailedFakeAgentRuntime())
    assert service2.start(max_tasks=1, force=True) == 0

    # Task is still in todo, not escalated yet
    assert (git_repo / ".jri" / "tasks" / "todo" / "failing-task.md").exists()

    # Third run succeeds (task is still retryable)
    success_client = SuccessfulFakeAgentRuntime()
    service3 = JriService(git_repo, agent_runtime=success_client)
    assert service3.start(max_tasks=1, force=True) == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "failing-task.md").exists()
    assert len(success_client.calls) == 1


def test_failed_task_can_keep_retrying_without_escalation(git_repo: Path) -> None:
    """Repeated failures keep the task in todo until a later successful retry."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="failing-task",
        title="Failing task",
        priority=0,
        assignee="Ralph",
        body="This will fail three times.",
    )
    git(git_repo, "add", ".jri/tasks/todo/failing-task.md")
    git(git_repo, "commit", "-m", "add failing task")

    # First failure
    service1 = JriService(git_repo, agent_runtime=FailedFakeAgentRuntime())
    assert service1.start(max_tasks=1, force=True) == 0

    # Second failure
    service2 = JriService(git_repo, agent_runtime=FailedFakeAgentRuntime())
    assert service2.start(max_tasks=1, force=True) == 0

    # Third failure still leaves the task retryable later
    fail_client3 = FailedFakeAgentRuntime()
    service3 = JriService(git_repo, agent_runtime=fail_client3)
    assert service3.start(max_tasks=1, force=True) == 0

    # Original task is back in todo and no Human task was generated
    assert (git_repo / ".jri" / "tasks" / "todo" / "failing-task.md").exists()
    todo_tasks = list_tasks(git_repo / ".jri" / "tasks" / "todo")
    human_tasks = [t for t in todo_tasks if t.metadata.assignee == "Human"]
    assert human_tasks == []
    original = parse_task_file(git_repo / ".jri" / "tasks" / "todo" / "failing-task.md")
    assert original.metadata.depends_on == []

    # Attempt history records three failures
    state = read_json(git_repo / ".jri" / "state.json")
    attempts = cast(list[dict[str, object]], state["attempts"])
    failed_for_task = [
        a
        for a in attempts
        if a.get("task_slug") == "failing-task" and a.get("result") == "failed"
    ]
    assert len(failed_for_task) == 3

    # Subsequent start still retries the task
    success_client = SuccessfulFakeAgentRuntime()
    service4 = JriService(git_repo, agent_runtime=success_client)
    assert service4.start(max_tasks=1, force=True) == 1
    assert len(success_client.calls) == 1


def test_failed_task_recovery_logs_failure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="failing-task",
        title="Failing task",
        priority=0,
        assignee="Ralph",
        body="This will fail.",
    )
    git(git_repo, "add", ".jri/tasks/todo/failing-task.md")
    git(git_repo, "commit", "-m", "add failing task")

    client = FailedFakeAgentRuntime()
    service = JriService(git_repo, agent_runtime=client)

    monkeypatch.setattr(
        JriService,
        "_reset_runtime_state",
        lambda self_: (_ for _ in ()).throw(
            OSError("simulated reset failure during recovery")
        ),
    )

    completed = service.start(max_tasks=1, force=True)

    assert completed == 0
    assert (git_repo / ".jri" / "tasks" / "todo" / "failing-task.md").exists()

    failure_log_path = git_repo / ".jri" / "logs" / "recovery-failures.log"
    assert failure_log_path.exists()
    failure_log = failure_log_path.read_text(encoding="utf-8")
    assert "event=recovery-failure" in failure_log
    assert "task=failing-task" in failure_log
    assert "phase=recover-failed-task" in failure_log
    assert "error_type=OSError" in failure_log
    assert "simulated reset failure during recovery" in failure_log


def test_needs_human_recovery_logs_failure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="needs-human-task",
        title="Needs human task",
        priority=0,
        assignee="Ralph",
        body="This will need human help.",
    )
    git(git_repo, "add", ".jri/tasks/todo/needs-human-task.md")
    git(git_repo, "commit", "-m", "add needs human task")

    client = NeedsHumanFakeAgentRuntime()
    service = JriService(git_repo, agent_runtime=client)

    monkeypatch.setattr(
        JriService,
        "_reset_runtime_state",
        lambda self_: (_ for _ in ()).throw(
            OSError("simulated reset failure during recovery")
        ),
    )

    completed = service.start(max_tasks=1, force=True)

    assert completed == 0

    failure_log_path = git_repo / ".jri" / "logs" / "recovery-failures.log"
    assert failure_log_path.exists()
    failure_log = failure_log_path.read_text(encoding="utf-8")
    assert "event=recovery-failure" in failure_log
    assert "task=needs-human-task" in failure_log
    assert "phase=recover-needs-human-task" in failure_log
    assert "error_type=OSError" in failure_log
    assert "simulated reset failure during recovery" in failure_log


def test_stale_task_recovery_logs_failure_and_propagates_error(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="stale-task",
        title="Stale task",
        priority=0,
        assignee="Ralph",
        body="Was interrupted.",
    )
    git(git_repo, "add", ".jri/tasks/doing/stale-task.md")
    git(git_repo, "commit", "-m", "seed stale task")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    import jri.core.service as service_module

    monkeypatch.setattr(
        service_module,
        "move_task",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("simulated move failure")),
    )

    with pytest.raises(OSError, match="simulated move failure"):
        service.start(max_tasks=1, force=True)

    failure_log_path = git_repo / ".jri" / "logs" / "recovery-failures.log"
    assert failure_log_path.exists()
    failure_log = failure_log_path.read_text(encoding="utf-8")
    assert "event=recovery-failure" in failure_log
    assert "task=stale-task" in failure_log
    assert "phase=recover-stale-task" in failure_log
    assert "error_type=OSError" in failure_log


def test_state_is_understandable_after_partial_recovery_failure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="failing-task",
        title="Failing task",
        priority=0,
        assignee="Ralph",
        body="This will fail.",
    )
    git(git_repo, "add", ".jri/tasks/todo/failing-task.md")
    git(git_repo, "commit", "-m", "add failing task")

    client = FailedFakeAgentRuntime()
    service = JriService(git_repo, agent_runtime=client)

    monkeypatch.setattr(
        JriService,
        "_reset_runtime_state",
        lambda self_: (_ for _ in ()).throw(
            OSError("simulated reset failure during recovery")
        ),
    )

    completed = service.start(max_tasks=1, force=True)

    assert completed == 0

    # Task is back in todo (checkout and branch cleanup succeeded before reset)
    assert (git_repo / ".jri" / "tasks" / "todo" / "failing-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "failing-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "done" / "failing-task.md").exists()

    # Git is on default branch (checkout succeeded before reset)
    assert git(git_repo, "branch", "--show-current") == "main"

    # State is valid and loadable
    state = read_json(git_repo / ".jri" / "state.json")
    assert "attempts" in state

    # Attempt records the failure
    attempts = cast(list[dict[str, object]], state["attempts"])
    assert len(attempts) == 1
    assert attempts[0]["task_slug"] == "failing-task"
    assert attempts[0]["result"] == "failed"

    # Recovery failure log explains what happened
    failure_log_path = git_repo / ".jri" / "logs" / "recovery-failures.log"
    assert failure_log_path.exists()
    failure_log = failure_log_path.read_text(encoding="utf-8")
    assert "event=recovery-failure" in failure_log
    assert "task=failing-task" in failure_log
    assert "phase=recover-failed-task" in failure_log


def test_successful_task_saves_diff_artifact(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    completed = service.start(max_tasks=1, force=True)

    assert completed == 1
    diff_path = git_repo / ".jri" / "logs" / "diffs" / "implement-file.diff"
    assert diff_path.exists()
    diff_text = diff_path.read_text(encoding="utf-8")
    assert "implemented.txt" in diff_text
    assert "+implemented" in diff_text


def test_diff_artifact_is_created_for_recovered_completion(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="task-a",
        title="Task A",
        priority=0,
        assignee="Ralph",
        body="Complete task A.",
    )
    write_task(
        git_repo,
        status="todo",
        slug="task-b",
        title="Task B",
        priority=1,
        assignee="Ralph",
        body="Complete task B.",
    )
    git(git_repo, "add", ".jri/tasks/todo")
    git(git_repo, "commit", "-m", "add retry tasks")

    first_client = SuccessfulFakeAgentRuntime()
    first_service = JriService(git_repo, agent_runtime=first_client)

    def interrupted_save_diff(task_slug: str) -> None:
        raise KeyboardInterrupt("simulated interruption during diff save")

    monkeypatch.setattr(first_service, "_save_diff_artifact", interrupted_save_diff)

    with pytest.raises(KeyboardInterrupt, match="simulated interruption"):
        first_service.start(max_tasks=1, force=True)

    assert (git_repo / ".jri" / "tasks" / "done" / "task-a.md").exists()
    diff_path = git_repo / ".jri" / "logs" / "diffs" / "task-a.diff"
    assert not diff_path.exists()

    retry_service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    assert retry_service.start(max_tasks=1, force=True) == 1

    assert diff_path.exists()
    diff_text = diff_path.read_text(encoding="utf-8")
    assert "implemented.txt" in diff_text


def test_successful_task_records_timeline_events(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    from jri.core.timeline import TimelineStore

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    completed = service.start(max_tasks=1, force=True)

    assert completed == 1
    timeline_path = git_repo / ".jri" / "logs" / "timeline.jsonl"
    assert timeline_path.exists()
    store = TimelineStore(timeline_path)
    events = store.read()
    event_types = [e.event for e in events]
    assert "attempt_started" in event_types
    assert "task_completed" in event_types
    started_events = [e for e in events if e.event == "attempt_started"]
    assert len(started_events) == 1
    assert started_events[0].task == "implement-file"
    completed_events = [e for e in events if e.event == "task_completed"]
    assert len(completed_events) == 1
    assert completed_events[0].task == "implement-file"


def test_failed_task_records_timeline_events(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="failing-task",
        title="Failing task",
        priority=0,
        assignee="Ralph",
        body="This will fail.",
    )
    git(git_repo, "add", ".jri/tasks/todo/failing-task.md")
    git(git_repo, "commit", "-m", "add failing task")

    from jri.core.timeline import TimelineStore

    service = JriService(git_repo, agent_runtime=FailedFakeAgentRuntime())

    completed = service.start(max_tasks=1, force=True)

    assert completed == 0
    timeline_path = git_repo / ".jri" / "logs" / "timeline.jsonl"
    assert timeline_path.exists()
    store = TimelineStore(timeline_path)
    events = store.read()
    event_types = [e.event for e in events]
    assert "attempt_started" in event_types
    assert "task_failed" in event_types
    assert "recovery_completed" in event_types
    failed_events = [e for e in events if e.event == "task_failed"]
    assert len(failed_events) == 1
    assert failed_events[0].task == "failing-task"


def test_needs_human_task_records_timeline_events(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="needs-human-task",
        title="Needs human task",
        priority=0,
        assignee="Ralph",
        body="This will need human help.",
    )
    git(git_repo, "add", ".jri/tasks/todo/needs-human-task.md")
    git(git_repo, "commit", "-m", "add needs human task")

    from jri.core.timeline import TimelineStore

    service = JriService(git_repo, agent_runtime=NeedsHumanFakeAgentRuntime())

    completed = service.start(max_tasks=1, force=True)

    assert completed == 0
    timeline_path = git_repo / ".jri" / "logs" / "timeline.jsonl"
    assert timeline_path.exists()
    store = TimelineStore(timeline_path)
    events = store.read()
    event_types = [e.event for e in events]
    assert "attempt_started" in event_types
    assert "task_needs_human" in event_types
    assert "recovery_completed" in event_types


def test_timeline_cli_shows_events(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    assert service.start(max_tasks=1, force=True) == 1

    rc = run_cli(["timeline"], cwd=git_repo)
    assert rc == 0
    output = capsys.readouterr().out
    assert "attempt_started" in output
    assert "task_completed" in output
    assert "implement-file" in output


def test_timeline_cli_outputs_jsonl(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    assert service.start(max_tasks=1, force=True) == 1

    # Flush task header/footer output before CLI call
    capsys.readouterr()

    rc = run_cli(["timeline", "--json"], cwd=git_repo)
    assert rc == 0
    output = capsys.readouterr().out
    import json

    for line in output.strip().splitlines():
        parsed = json.loads(line)
        assert "ts" in parsed
        assert "event" in parsed


def test_timeline_cli_reports_empty_history(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    capsys.readouterr()

    assert run_cli(["timeline"], cwd=git_repo) == 0

    assert "No timeline events recorded." in capsys.readouterr().out


def test_timeline_cli_outputs_empty_json_array_when_history_is_empty(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    capsys.readouterr()

    assert run_cli(["timeline", "--json"], cwd=git_repo) == 0

    assert capsys.readouterr().out.strip() == "[]"


def test_timeline_cli_outputs_empty_json_array_for_unmatched_task_filter(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from jri.core.timeline import TimelineEvent, TimelineStore

    assert run_cli(["init"], cwd=git_repo) == 0
    TimelineStore(git_repo / ".jri" / "logs" / "timeline.jsonl").record(
        TimelineEvent(ts="2026-05-02T00:00:00Z", event="task_completed", task="task-a")
    )
    capsys.readouterr()

    assert run_cli(["timeline", "--task", "task-b", "--json"], cwd=git_repo) == 0

    assert capsys.readouterr().out.strip() == "[]"


def test_reset_cli_aborts_on_negative_confirmation_in_process(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    assert service.start(max_tasks=1, force=True) == 1
    monkeypatch.setattr("builtins.input", lambda: "n")
    capsys.readouterr()

    assert run_cli(["reset"], cwd=git_repo) == 1

    captured = capsys.readouterr()
    assert "Are you sure? [y/N]" in captured.out
    assert "Reset aborted." in captured.err
    assert (git_repo / ".jri" / "tasks" / "done" / "implement-file.md").exists()


def test_task_limit_stops_loop_after_n_tasks(git_repo: Path) -> None:
    """Loop stops after completing the configured number of tasks."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="task-a",
        title="Task A",
        priority=0,
        assignee="Ralph",
        body="Complete task A.",
        acceptance_criteria=["Task A is done"],
    )
    write_task(
        git_repo,
        status="todo",
        slug="task-b",
        title="Task B",
        priority=1,
        assignee="Ralph",
        body="Complete task B.",
        acceptance_criteria=["Task B is done"],
    )
    write_task(
        git_repo,
        status="todo",
        slug="task-c",
        title="Task C",
        priority=2,
        assignee="Ralph",
        body="Complete task C.",
        acceptance_criteria=["Task C is done"],
    )
    git(git_repo, "add", ".jri/tasks/todo")
    git(git_repo, "commit", "-m", "add three tasks")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    # Only run 2 tasks even though there are 3 tasks
    completed = service.start(max_tasks=2, force=True)

    assert completed == 2
    # First two tasks should be done
    assert (git_repo / ".jri" / "tasks" / "done" / "task-a.md").exists()
    assert (git_repo / ".jri" / "tasks" / "done" / "task-b.md").exists()
    # Third task should still be in todo
    assert (git_repo / ".jri" / "tasks" / "todo" / "task-c.md").exists()


def test_task_limit_records_timeline_event(git_repo: Path) -> None:
    """Stopping due to task limit is recorded in timeline."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="task-a",
        title="Task A",
        priority=0,
        assignee="Ralph",
        body="Complete task A.",
        acceptance_criteria=["Task A is done"],
    )
    write_task(
        git_repo,
        status="todo",
        slug="task-b",
        title="Task B",
        priority=1,
        assignee="Ralph",
        body="Complete task B.",
        acceptance_criteria=["Task B is done"],
    )
    git(git_repo, "add", ".jri/tasks/todo")
    git(git_repo, "commit", "-m", "add two tasks")

    from jri.core.timeline import TimelineStore

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.start(max_tasks=1, force=True)

    timeline_path = git_repo / ".jri" / "logs" / "timeline.jsonl"
    store = TimelineStore(timeline_path)
    events = store.read()

    loop_stopped_events = [e for e in events if e.event == "loop_stopped"]
    assert len(loop_stopped_events) == 1
    assert loop_stopped_events[0].detail is not None
    assert loop_stopped_events[0].detail.get("reason") == "task_limit"
    assert loop_stopped_events[0].detail.get("limit") == 1


class SlowFakeAgentRuntime(FakeAgentRuntime):
    """Simulates a task that takes a long time to complete."""

    def __init__(self, delay_seconds: int = 0) -> None:
        super().__init__(model=None)
        self.calls: list[tuple[str, Path]] = []
        self.delay_seconds = delay_seconds

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        import time

        self.calls.append((prompt, log_path))
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)
        (root / "implemented.txt").write_text("implemented\n", encoding="utf-8")
        log_path.write_text("fake slow run\n", encoding="utf-8")
        return AgentRunResult(returncode=0, session_id="ses_slow", result="completed")

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text('{"session": "slow"}\n', encoding="utf-8")


def test_task_timeout_stops_slow_task(git_repo: Path) -> None:
    """Task that exceeds timeout is stopped and marked as failed."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="slow-task",
        title="Slow task",
        priority=0,
        assignee="Ralph",
        body="This task takes too long.",
        acceptance_criteria=["Task completes"],
    )
    git(git_repo, "add", ".jri/tasks/todo/slow-task.md")
    git(git_repo, "commit", "-m", "add slow task")

    # Task takes 2 seconds but timeout is 1 second
    client = SlowFakeAgentRuntime(delay_seconds=2)
    service = JriService(git_repo, agent_runtime=client)

    completed = service.start(max_tasks=1, task_timeout=1, force=True)

    # Task should not have completed successfully
    assert completed == 0
    # Task should be back in todo after timeout recovery
    assert (git_repo / ".jri" / "tasks" / "todo" / "slow-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "slow-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "done" / "slow-task.md").exists()
    attempts = cast(
        list[dict[str, object]],
        read_json(git_repo / ".jri" / "state.json")["attempts"],
    )
    assert attempts[0]["result"] == "timeout"


def test_task_timeout_records_timeline_event(git_repo: Path) -> None:
    """Task timeout is recorded in timeline with limit information."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="slow-task",
        title="Slow task",
        priority=0,
        assignee="Ralph",
        body="This task takes too long.",
        acceptance_criteria=["Task completes"],
    )
    git(git_repo, "add", ".jri/tasks/todo/slow-task.md")
    git(git_repo, "commit", "-m", "add slow task")

    from jri.core.timeline import TimelineStore

    client = SlowFakeAgentRuntime(delay_seconds=2)
    service = JriService(git_repo, agent_runtime=client)

    service.start(max_tasks=1, task_timeout=1, force=True)

    timeline_path = git_repo / ".jri" / "logs" / "timeline.jsonl"
    store = TimelineStore(timeline_path)
    events = store.read()

    # Should have task_failed event with timeout reason
    timeout_events = [
        e
        for e in events
        if e.event == "task_failed"
        and e.detail is not None
        and e.detail.get("reason") == "task_timeout"
    ]
    assert len(timeout_events) == 1
    assert timeout_events[0].detail is not None
    assert timeout_events[0].detail.get("limit_seconds") == 1

    # Should have loop_stopped event
    loop_stopped_events = [e for e in events if e.event == "loop_stopped"]
    assert len(loop_stopped_events) == 1
    assert loop_stopped_events[0].detail is not None
    assert loop_stopped_events[0].detail.get("reason") == "task_timeout"
    assert loop_stopped_events[0].detail.get("limit_seconds") == 1


def test_successful_task_run_persists_logs(git_repo: Path) -> None:
    """Verify that a successful task run creates durable per-task logs."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="log-test-task",
        title="Log test task",
        priority=0,
        assignee="Ralph",
        body="Create a file to verify logs are persisted.",
        acceptance_criteria=["log-test.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/todo/log-test-task.md")
    git(git_repo, "commit", "-m", "add log test task")

    from jri.core.timeline import TimelineStore

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    completed = service.start(max_tasks=1, force=True)

    assert completed == 1

    # Verify Ralph log file exists
    ralph_logs_dir = git_repo / ".jri" / "logs" / "ralph"
    assert ralph_logs_dir.exists()
    log_files = list(ralph_logs_dir.glob("*.log"))
    assert len(log_files) == 1
    ralph_log = log_files[0]
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z-log-test-task\.log$",
        ralph_log.name,
    )
    assert ralph_log.read_text(encoding="utf-8") == "fake run\n"

    # Verify timeline has attempt_started event with log_path
    timeline_path = git_repo / ".jri" / "logs" / "timeline.jsonl"
    assert timeline_path.exists()
    store = TimelineStore(timeline_path)
    events = store.read()

    started_events = [e for e in events if e.event == "attempt_started"]
    assert len(started_events) == 1
    assert started_events[0].detail is not None
    assert "log_path" in started_events[0].detail
    log_path_in_event = started_events[0].detail["log_path"]
    assert isinstance(log_path_in_event, str)
    assert "ralph" in log_path_in_event
    assert ".log" in log_path_in_event


def test_failed_task_run_persists_logs(git_repo: Path) -> None:
    """Verify that a failed task run creates durable per-task logs."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="failing-log-task",
        title="Failing log task",
        priority=0,
        assignee="Ralph",
        body="This will fail but logs should be persisted.",
    )
    git(git_repo, "add", ".jri/tasks/todo/failing-log-task.md")
    git(git_repo, "commit", "-m", "add failing log task")

    from jri.core.timeline import TimelineStore

    service = JriService(git_repo, agent_runtime=FailedFakeAgentRuntime())

    completed = service.start(max_tasks=1, force=True)

    assert completed == 0

    # Verify Ralph log file exists even for failed run
    ralph_logs_dir = git_repo / ".jri" / "logs" / "ralph"
    assert ralph_logs_dir.exists()
    log_files = list(ralph_logs_dir.glob("*.log"))
    assert len(log_files) == 1
    ralph_log = log_files[0]
    assert "fake failed run" in ralph_log.read_text(encoding="utf-8")

    # Verify timeline has both attempt_started and task_failed events
    timeline_path = git_repo / ".jri" / "logs" / "timeline.jsonl"
    assert timeline_path.exists()
    store = TimelineStore(timeline_path)
    events = store.read()

    started_events = [e for e in events if e.event == "attempt_started"]
    assert len(started_events) == 1
    assert started_events[0].detail is not None
    assert "log_path" in started_events[0].detail

    failed_events = [e for e in events if e.event == "task_failed"]
    assert len(failed_events) == 1
    assert failed_events[0].task == "failing-log-task"

    # Verify recovery was logged
    recovery_events = [e for e in events if e.event == "recovery_completed"]
    assert len(recovery_events) == 1


def test_needs_human_task_run_persists_logs(git_repo: Path) -> None:
    """Verify that a needs-human task run creates durable per-task logs."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="needs-human-log-task",
        title="Needs human log task",
        priority=0,
        assignee="Ralph",
        body="This will need human help but logs should be persisted.",
    )
    git(git_repo, "add", ".jri/tasks/todo/needs-human-log-task.md")
    git(git_repo, "commit", "-m", "add needs human log task")

    from jri.core.timeline import TimelineStore

    service = JriService(git_repo, agent_runtime=NeedsHumanFakeAgentRuntime())

    completed = service.start(max_tasks=1, force=True)

    assert completed == 0

    # Verify Ralph log file exists
    ralph_logs_dir = git_repo / ".jri" / "logs" / "ralph"
    assert ralph_logs_dir.exists()
    log_files = list(ralph_logs_dir.glob("*.log"))
    assert len(log_files) == 1
    ralph_log = log_files[0]
    assert "fake needs-human run" in ralph_log.read_text(encoding="utf-8")

    # Verify timeline has attempt_started and task_needs_human events
    timeline_path = git_repo / ".jri" / "logs" / "timeline.jsonl"
    assert timeline_path.exists()
    store = TimelineStore(timeline_path)
    events = store.read()

    started_events = [e for e in events if e.event == "attempt_started"]
    assert len(started_events) == 1
    assert started_events[0].detail is not None
    assert "log_path" in started_events[0].detail

    needs_human_events = [e for e in events if e.event == "task_needs_human"]
    assert len(needs_human_events) == 1
    assert needs_human_events[0].task == "needs-human-log-task"

    # Verify the Human task references the log path
    todo_tasks = list_tasks(git_repo / ".jri" / "tasks" / "todo")
    human_tasks = [t for t in todo_tasks if t.metadata.assignee == "Human"]
    assert len(human_tasks) == 1
    assert ".jri/logs/ralph/" in human_tasks[0].body


def test_timeline_records_stderr_warnings(git_repo: Path) -> None:
    """Verify that stderr-only messages are captured as timeline events."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="warning-test-task",
        title="Warning test task",
        priority=0,
        assignee="Ralph",
        body="Test that warnings are captured.",
        acceptance_criteria=["warning-test.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/todo/warning-test-task.md")
    git(git_repo, "commit", "-m", "add warning test task")

    # Create a client that returns warnings
    class WarningFakeAgentRuntime(SuccessfulFakeAgentRuntime):
        def run_ralph_task(
            self,
            *,
            root: Path,
            prompt: str,
            log_path: Path,
            result_path: Path,
            on_start: object | None = None,
            timeout: int | None = None,
        ) -> AgentRunResult:
            result = super().run_ralph_task(
                root=root,
                prompt=prompt,
                log_path=log_path,
                result_path=result_path,
                on_start=on_start,
                timeout=timeout,
            )
            return AgentRunResult(
                returncode=result.returncode,
                session_id=result.session_id,
                result=result.result,
                payload=result.payload,
                warnings=["Test warning message"],
            )

    from jri.core.timeline import TimelineStore

    service = JriService(git_repo, agent_runtime=WarningFakeAgentRuntime())

    service.start(max_tasks=1, force=True)

    # Verify timeline has stderr_warning event
    timeline_path = git_repo / ".jri" / "logs" / "timeline.jsonl"
    store = TimelineStore(timeline_path)
    events = store.read()

    warning_events = [e for e in events if e.event == "stderr_warning"]
    assert len(warning_events) == 1
    assert warning_events[0].task == "warning-test-task"
    assert warning_events[0].detail is not None
    assert warning_events[0].detail.get("message") == "Test warning message"


class StopAfterFirstTaskAgentRuntime(SuccessfulFakeAgentRuntime):
    """Client that creates a stop signal after completing the first task."""

    def __init__(self, signals_dir: Path) -> None:
        super().__init__()
        self.signals_dir = signals_dir
        self._call_count = 0

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        self._call_count += 1
        result = super().run_ralph_task(
            root=root,
            prompt=prompt,
            log_path=log_path,
            result_path=result_path,
            on_start=on_start,
            timeout=timeout,
        )
        # Create stop signal after first task completes
        if self._call_count == 1:
            self.signals_dir.mkdir(parents=True, exist_ok=True)
            (self.signals_dir / "stop").write_text(
                "stop after first task\n", encoding="utf-8"
            )
        return result


def test_stop_during_active_work_stops_after_task(git_repo: Path) -> None:
    """Stop signal created during iteration stops loop after current iteration."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="task-a",
        title="Task A",
        priority=0,
        assignee="Ralph",
        body="Complete task A.",
        acceptance_criteria=["Task A is done"],
    )
    write_task(
        git_repo,
        status="todo",
        slug="task-b",
        title="Task B",
        priority=1,
        assignee="Ralph",
        body="Complete task B.",
        acceptance_criteria=["Task B is done"],
    )
    git(git_repo, "add", ".jri/tasks/todo")
    git(git_repo, "commit", "-m", "add two tasks")

    signals_dir = git_repo / ".jri" / "signals"
    service = JriService(
        git_repo, agent_runtime=StopAfterFirstTaskAgentRuntime(signals_dir)
    )

    # Run the loop - client will create stop signal during first iteration
    completed = service.start(max_tasks=10, force=True)

    # Should have completed only 1 task before stopping
    assert completed == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "task-a.md").exists()
    # Second task should still be in todo
    assert (git_repo / ".jri" / "tasks" / "todo" / "task-b.md").exists()
    # Stop signal should be consumed (deleted)
    assert not (git_repo / ".jri" / "signals" / "stop").exists()


def test_stop_signal_consumed_on_start(git_repo: Path) -> None:
    """Stop signal present at start is consumed immediately before processing."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    # Create stop signal file directly
    signals_dir = git_repo / ".jri" / "signals"
    signals_dir.mkdir(parents=True, exist_ok=True)
    stop_signal = signals_dir / "stop"
    stop_signal.write_text("pre-existing stop signal\n", encoding="utf-8")
    assert stop_signal.exists()

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    # Start the service - stop signal should be consumed at start
    completed = service.start(max_tasks=1, force=True)

    # Task should complete normally since stop signal is consumed at start
    assert completed == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "implement-file.md").exists()
    # Stop signal should be deleted
    assert not stop_signal.exists()


def test_stop_reason_preserved_in_signal(git_repo: Path) -> None:
    """Stop signal preserves the reason text provided."""
    assert run_cli(["init"], cwd=git_repo) == 0

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    # Create stop signal with a specific reason
    reason = "Scheduled maintenance window at 2024-01-15 02:00 UTC"
    service.stop(reason)

    stop_signal = git_repo / ".jri" / "signals" / "stop"
    assert stop_signal.exists()
    # Verify the reason is preserved in the signal file
    content = stop_signal.read_text(encoding="utf-8")
    assert content == f"{reason}\n"


def test_halt_raises_error_when_no_tracked_process(git_repo: Path) -> None:
    """Halt raises JriError when no process is currently tracked."""
    assert run_cli(["init"], cwd=git_repo) == 0

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    # Verify halt raises error when no process is tracked
    with pytest.raises(JriError, match="no Ralph process is currently tracked"):
        service.halt()


def test_halt_clears_process_state(git_repo: Path) -> None:
    """Halt clears the process state from state.json after terminating."""
    assert run_cli(["init"], cwd=git_repo) == 0

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    # Save a fake process to state (simulating a running process)
    service.state_store.save_process(
        loop_pid=_dead_pid(),
        child_pid=None,
        log_path=git_repo / ".jri" / "logs" / "ralph" / "fake.log",
        detached=True,
    )

    # Verify process state exists
    state_before = read_json(git_repo / ".jri" / "state.json")
    assert state_before is not None
    assert state_before.get("process") is not None
    process_before = cast(dict[str, object], state_before["process"])
    assert process_before["loop_pid"] is not None

    # Halt should clear the process state
    service.halt()

    # Verify process state is cleared
    state_after = read_json(git_repo / ".jri" / "state.json")
    assert state_after.get("process") is None


def test_stop_then_start_recovery_consistency(git_repo: Path) -> None:
    """Stop during run allows clean recovery on next start."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="task-a",
        title="Task A",
        priority=0,
        assignee="Ralph",
        body="Complete task A.",
        acceptance_criteria=["Task A is done"],
    )
    write_task(
        git_repo,
        status="todo",
        slug="task-b",
        title="Task B",
        priority=1,
        assignee="Ralph",
        body="Complete task B.",
        acceptance_criteria=["Task B is done"],
    )
    git(git_repo, "add", ".jri/tasks/todo")
    git(git_repo, "commit", "-m", "add two tasks")

    signals_dir = git_repo / ".jri" / "signals"

    # First service instance - client creates stop signal during first iteration
    service1 = JriService(
        git_repo, agent_runtime=StopAfterFirstTaskAgentRuntime(signals_dir)
    )

    # Run - completes one task then stops
    completed1 = service1.start(max_tasks=10, force=True)
    assert completed1 == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "task-a.md").exists()
    assert (git_repo / ".jri" / "tasks" / "todo" / "task-b.md").exists()

    # Second service instance - should continue with remaining tasks
    service2 = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    completed2 = service2.start(max_tasks=10, force=True)

    # Should complete the second task
    assert completed2 == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "task-b.md").exists()


def test_halt_then_start_recovery_consistency(git_repo: Path) -> None:
    """Halt during work allows recovery with interrupted attempt marked."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="interrupted-task",
        title="Interrupted task",
        priority=0,
        assignee="Ralph",
        body="This task was interrupted.",
        acceptance_criteria=["Task is done"],
    )
    git(git_repo, "add", ".jri/tasks/doing/interrupted-task.md")
    git(git_repo, "commit", "-m", "seed interrupted task")

    # Create an active attempt simulating interrupted work
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    interrupted_attempt = AttemptState(
        number=1,
        task_slug="interrupted-task",
        branch="ralph",
        started_at=1234567890,
        log_path=".jri/logs/ralph/1-interrupted.log",
    )
    service.state_store.save(
        State(
            started_at=1234567890,
            branch="main",
            active_attempt=interrupted_attempt,
            attempts=[interrupted_attempt],
        )
    )

    # Start should recover the stale iteration and then continue with the task
    # Recovery moves task back to todo and marks first attempt as interrupted
    # Then the task is picked up and completed as a new attempt
    completed = service.start(max_tasks=1, force=True)

    # Task should be completed (recovered + new attempt = 2 total attempts, 1 completed)
    assert completed == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "interrupted-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "interrupted-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "todo" / "interrupted-task.md").exists()

    # Verify attempts - first is interrupted, second is completed
    attempts = cast(
        list[dict[str, object]],
        read_json(git_repo / ".jri" / "state.json")["attempts"],
    )
    assert len(attempts) == 2
    assert attempts[0]["result"] == "interrupted"
    assert attempts[0]["task_slug"] == "interrupted-task"
    assert attempts[1]["result"] == "completed"
    assert attempts[1]["task_slug"] == "interrupted-task"


def test_stop_signal_persists_across_invocations(git_repo: Path) -> None:
    """Stop signal persists until consumed by a loop iteration."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="task-a",
        title="Task A",
        priority=0,
        assignee="Ralph",
        body="Complete task A.",
        acceptance_criteria=["Task A is done"],
    )
    git(git_repo, "add", ".jri/tasks/todo/task-a.md")
    git(git_repo, "commit", "-m", "add task")

    # Create service and stop signal
    service1 = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service1.stop("persisted stop signal")

    stop_signal = git_repo / ".jri" / "signals" / "stop"
    assert stop_signal.exists()

    # Create a new service instance (simulating new invocation)
    # Stop signal should still exist
    service2 = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    assert stop_signal.exists()

    # Run loop - signal should be consumed
    completed = service2.start(max_tasks=1, force=True)
    assert completed == 1
    assert not stop_signal.exists()


class ExportFailingFakeAgentRuntime(FakeAgentRuntime):
    """Simulates export failures."""

    def __init__(self) -> None:
        super().__init__(model=None)
        self.calls: list[tuple[str, Path]] = []

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        self.calls.append((prompt, log_path))
        (root / "implemented.txt").write_text("implemented\n", encoding="utf-8")
        log_path.write_text("fake run\n", encoding="utf-8")
        return AgentRunResult(
            returncode=0, session_id="ses_export_fail", result="completed"
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        raise JriError(f"Export failed for session {session_id}")


def test_export_failure_is_visible_in_timeline(git_repo: Path) -> None:
    """Export failures are recorded in timeline and not silently ignored."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="export-fail-task",
        title="Export fail task",
        priority=0,
        assignee="Ralph",
        body="Task where export will fail.",
        acceptance_criteria=["Task completes despite export failure"],
    )
    git(git_repo, "add", ".jri/tasks/todo/export-fail-task.md")
    git(git_repo, "commit", "-m", "add export fail task")

    from jri.core.timeline import TimelineStore

    client = ExportFailingFakeAgentRuntime()
    service = JriService(git_repo, agent_runtime=client)

    # Task should still complete even if export fails
    completed = service.start(max_tasks=1, force=True)
    assert completed == 1

    # Verify timeline has export_failed event
    timeline_path = git_repo / ".jri" / "logs" / "timeline.jsonl"
    assert timeline_path.exists()
    store = TimelineStore(timeline_path)
    events = store.read()

    export_failed_events = [e for e in events if e.event == "export_failed"]
    assert len(export_failed_events) == 1
    assert export_failed_events[0].task == "export-fail-task"
    assert export_failed_events[0].detail is not None
    assert export_failed_events[0].detail.get("session_id") == "ses_export_fail"
    assert "Export failed" in str(export_failed_events[0].detail.get("error", ""))


def test_export_failure_during_failed_recovery_is_visible(git_repo: Path) -> None:
    """Export failures during failed-task recovery are recorded in timeline."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="failing-task",
        title="Failing task",
        priority=0,
        assignee="Ralph",
        body="This task will fail.",
    )
    git(git_repo, "add", ".jri/tasks/todo/failing-task.md")
    git(git_repo, "commit", "-m", "add failing task")

    from jri.core.timeline import TimelineStore

    # Create a client that fails at the runtime level and also fails on export
    class FailingWithExportFail(FakeAgentRuntime):
        def __init__(self) -> None:
            super().__init__(model=None)
            self.call_count = 0

        def run_ralph_task(
            self,
            *,
            root: Path,
            prompt: str,
            log_path: Path,
            result_path: Path,
            on_start: object | None = None,
            timeout: int | None = None,
        ) -> AgentRunResult:
            self.call_count += 1
            log_path.write_text(f"failed run #{self.call_count}\n", encoding="utf-8")
            # Process exited cleanly, but the runtime marked the run failed.
            return AgentRunResult(
                returncode=0,
                session_id=f"ses_fail_{self.call_count}",
                result="failed",
            )

        def export_session(self, session_id: str, destination: Path) -> None:
            raise JriError(f"Export failed for {session_id}")

    client = FailingWithExportFail()
    service = JriService(git_repo, agent_runtime=client)
    service.start(max_tasks=1, force=True)

    # Verify timeline has export_failed events during recovery
    timeline_path = git_repo / ".jri" / "logs" / "timeline.jsonl"
    store = TimelineStore(timeline_path)
    events = store.read()

    export_failed_events = [e for e in events if e.event == "export_failed"]
    assert len(export_failed_events) == 1
    for event in export_failed_events:
        assert event.task == "failing-task"


# ---------------------------------------------------------------------------
# Pre-flight check tests
# ---------------------------------------------------------------------------


def test_start_stashes_dirty_workdir_with_force(git_repo: Path) -> None:
    """With force=True, dirty workdir is auto-stashed before the loop runs."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    # Dirty the workdir
    (git_repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    git(git_repo, "add", "dirty.txt")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    completed = service.start(max_tasks=1, force=True)

    assert completed == 1
    # The stash should have captured dirty.txt; verify stash list is non-empty
    stash_list = git(git_repo, "stash", "list")
    assert "stash@{0}" in stash_list


def test_start_allows_dirty_draft_tasks_without_force(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    write_task(
        git_repo,
        status="draft",
        slug="clarify-scope",
        title="Clarify scope",
        priority=1,
        assignee="Human",
        body="Capture open questions.",
    )

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    completed = service.start(max_tasks=1)

    assert completed == 1
    assert git(git_repo, "stash", "list") == ""
    assert (git_repo / ".jri" / "tasks" / "draft" / "clarify-scope.md").exists()


def test_start_force_stashes_when_dirty_paths_are_not_draft_only(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    write_task(
        git_repo,
        status="draft",
        slug="clarify-scope",
        title="Clarify scope",
        priority=1,
        assignee="Human",
        body="Capture open questions.",
    )
    (git_repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    git(git_repo, "add", "dirty.txt")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    completed = service.start(max_tasks=1, force=True)

    assert completed == 1
    stash_list = git(git_repo, "stash", "list")
    assert "stash@{0}" in stash_list


def test_start_switches_branch_with_force(git_repo: Path) -> None:
    """With force=True, wrong branch is auto-switched before the loop runs."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    # Switch to a feature branch
    git(git_repo, "checkout", "-b", "feature/x")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    completed = service.start(max_tasks=1, force=True)

    assert completed == 1
    assert git(git_repo, "branch", "--show-current") == "main"


def test_start_rejects_wrong_branch_confirmation(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")
    git(git_repo, "checkout", "-b", "feature/reject")
    monkeypatch.setattr("builtins.input", lambda: "n")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    with pytest.raises(JriError, match="aborted by user"):
        service.start(max_tasks=1, force=False)

    assert git(git_repo, "branch", "--show-current") == "feature/reject"
    assert (git_repo / ".jri" / "tasks" / "todo" / "implement-file.md").exists()


def test_follow_controls_poll_action_reads_ready_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controls = _FollowControls(enabled=True, fd=9)
    monkeypatch.setattr(
        "jri.core.service.select.select",
        lambda *args: ([sys.stdin], [], []),
    )
    monkeypatch.setattr("jri.core.service.os.read", lambda fd, size: b"D")

    assert controls.poll_action() == "detach"


def test_follow_controls_poll_action_ignores_unavailable_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _FollowControls(enabled=False).poll_action() is None
    assert _FollowControls(enabled=True, fd=None).poll_action() is None
    controls = _FollowControls(enabled=True, fd=9)
    monkeypatch.setattr(
        "jri.core.service.select.select",
        lambda *args: (_ for _ in ()).throw(OSError("bad fd")),
    )
    assert controls.poll_action() is None
    monkeypatch.setattr("jri.core.service.select.select", lambda *args: ([], [], []))
    assert controls.poll_action() is None
    monkeypatch.setattr(
        "jri.core.service.select.select",
        lambda *args: ([sys.stdin], [], []),
    )
    monkeypatch.setattr(
        "jri.core.service.os.read",
        lambda fd, size: (_ for _ in ()).throw(OSError("read failed")),
    )
    assert controls.poll_action() is None


def test_follow_controls_handle_empty_and_unarmed_halt_keys() -> None:
    controls = _FollowControls(enabled=True)

    assert controls.handle_key("") is None
    assert controls.handle_key("h") is None
    assert controls.handle_key("\n") is None
    assert controls.confirming_halt is True
    assert controls.halt_armed is False


def test_init_prompt_delete_recreates_existing_jri(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    leftover = git_repo / ".jri" / "custom.txt"
    leftover.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda: "d")

    JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime()).init(
        delete=False,
        commit_message="reinitialize jri",
    )

    assert not leftover.exists()
    assert (git_repo / ".jri" / "tasks" / "todo" / ".gitkeep").exists()


def test_init_prompt_eof_aborts_existing_jri(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    monkeypatch.setattr(
        "builtins.input",
        lambda: (_ for _ in ()).throw(EOFError),
    )

    with pytest.raises(JriError, match="initialization aborted"):
        JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime()).init(
            delete=False,
            commit_message="reinitialize jri",
        )


def test_chat_returns_nonzero_without_saving_new_session(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save_session("existing-session")
    monkeypatch.setattr("jri.core.service.launch_chat", lambda **kwargs: 17)

    assert service.chat([], fresh=False) == 17
    assert service.state_store.load().session == "existing-session"


def test_attach_rejects_missing_tracked_run(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0

    with pytest.raises(JriError, match="no Ralph run"):
        JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime()).attach()


def test_inspect_reports_missing_attempt_and_log_errors(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    with pytest.raises(JriError, match="no task attempts recorded"):
        service.inspect()
    service.state_store.save(
        State(
            attempts=[
                AttemptState(
                    number=1,
                    task_slug="task-a",
                    branch="ralph",
                    started_at=1,
                )
            ]
        )
    )
    with pytest.raises(JriError, match="has no saved log"):
        service.inspect("task-a")
    service.state_store.save(
        State(
            attempts=[
                AttemptState(
                    number=1,
                    task_slug="task-b",
                    branch="ralph",
                    started_at=1,
                    log_path=str(git_repo / ".jri" / "logs" / "missing.log"),
                )
            ]
        )
    )
    with pytest.raises(JriError, match="task log not found"):
        service.inspect("task-b")
    with pytest.raises(JriError, match="task 'task-c' has no recorded attempts"):
        service.inspect("task-c")


def test_inspect_prints_unknown_result_and_adds_missing_newline(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    log_path = git_repo / ".jri" / "logs" / "ralph" / "odd.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("raw output without newline", encoding="utf-8")
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    state = State(
        attempts=[
            AttemptState(
                number=1,
                task_slug="odd-task",
                branch="ralph",
                started_at=1,
                finished_at=2,
                log_path=str(log_path),
                result=cast(Any, "custom-result"),
            )
        ]
    )
    monkeypatch.setattr(service.state_store, "load", lambda: state)

    service.inspect("odd-task")

    output = capsys.readouterr().out
    assert "raw output without newline\ncustom-result\n" in output


def test_stop_cancel_is_noop_when_signal_is_missing(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    service.stop()
    assert service.paths.stop_signal_path.read_text(encoding="utf-8") == ""
    service.paths.stop_signal_path.unlink()
    service.cancel_stop()

    assert not service.paths.stop_signal_path.exists()


def test_status_wraps_task_parse_errors(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    bad_task = git_repo / ".jri" / "tasks" / "todo" / "bad.md"
    bad_task.write_text("not front matter\n", encoding="utf-8")

    with pytest.raises(JriError, match="frontmatter"):
        JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime()).status()


def test_ralph_status_summary_reports_current_task_and_stop_signal(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save_process(
        loop_pid=12345,
        child_pid=None,
        log_path=None,
        detached=False,
    )
    service.state_store.mark_task_started(task_slug="current", started_at=1)
    service.paths.stop_signal_path.parent.mkdir(parents=True, exist_ok=True)
    service.paths.stop_signal_path.write_text("stop\n", encoding="utf-8")
    monkeypatch.setattr(service, "_is_pid_alive", lambda pid: True)

    assert service.ralph_status_summary() == (
        "Ralph: running (attached) on current, stop requested"
    )


def test_private_task_selection_and_path_helpers(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    task_path = write_task(
        git_repo,
        status="draft",
        slug="draft-a",
        title="Draft A",
        priority=0,
        assignee="Ralph",
        body="Draft body.",
    )
    task = parse_task_file(task_path)

    assert service._status_paths("R  old.md -> new.md\n?? short") == [
        "new.md",
        "short",
    ]
    assert service._commit_paths(["missing", ".jri/tasks/draft/draft-a.md"]) == [
        ".jri/tasks/draft/draft-a.md"
    ]
    assert service._select_draft_tasks([task], []) == [task]
    with pytest.raises(JriError, match="draft task not found: missing"):
        service._select_draft_tasks([task], ["missing"])


def test_private_promotion_helpers_report_expected_errors(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    with pytest.raises(JriError, match="no draft tasks selected"):
        service._select_draft_tasks([], [])
    with pytest.raises(JriError, match="draft promotion must be approved"):
        service._validate_promotion_approval([])


def test_status_helpers_cover_promoted_dependencies(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="todo-a",
        title="Todo A",
        priority=0,
        assignee="Ralph",
        body="Todo body.",
        depends_on=["done-a"],
    )
    write_task(
        git_repo,
        status="done",
        slug="done-a",
        title="Done A",
        priority=0,
        assignee="Ralph",
        body="Done body.",
    )
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    assert service._promoted_task_slugs() == {"todo-a", "done-a"}
    assert service._promoted_task_deps()["todo-a"] == ["done-a"]


def test_write_template_skips_existing_files(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    learnings = git_repo / ".jri" / "learnings.md"
    learnings.write_text("keep me\n", encoding="utf-8")

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service._write_template_files((".jri/learnings.md",))

    assert learnings.read_text(encoding="utf-8") == "keep me\n"


def test_start_detached_rejects_existing_tracked_process(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save_process(
        loop_pid=12345,
        child_pid=None,
        log_path=None,
        detached=True,
    )

    with pytest.raises(JriError, match="already tracked"):
        service._start_detached(None, None, None, None, None, None, False)


def test_start_followable_returns_failure_when_child_exits_nonzero(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0

    class FailingDetachedProcess(FakeDetachedProcess):
        def wait(self) -> int:
            return 2

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    monkeypatch.setattr(
        "jri.core.service.subprocess.Popen",
        lambda *args, **kwargs: FailingDetachedProcess(222),
    )
    monkeypatch.setattr(service, "_follow_log", lambda *args, **kwargs: False)

    assert (
        service._start_followable(
            None,
            None,
            None,
            None,
            None,
            None,
            False,
            False,
        )
        == 1
    )


def test_start_followable_builds_all_optional_child_args(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    commands: list[list[str]] = []

    def fake_popen(*args: object, **kwargs: object) -> FakeDetachedProcess:
        commands.append(cast(list[str], args[0]))
        return FakeDetachedProcess(333)

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    monkeypatch.setattr("jri.core.service.subprocess.Popen", fake_popen)
    monkeypatch.setattr(service, "_follow_log", lambda *args, **kwargs: True)
    monkeypatch.setattr("jri.core.service.supports_color", lambda: True)

    assert (
        service._start_followable(
            3,
            "ralph-model",
            "validator-model",
            "general-model",
            "explore-model",
            60,
            True,
            True,
        )
        == 0
    )
    assert commands[0] == [
        sys.executable,
        "-m",
        "jri",
        "-n",
        "3",
        "--model",
        "ralph-model",
        "--validator-model",
        "validator-model",
        "--general-model",
        "general-model",
        "--explore-model",
        "explore-model",
        "--task-timeout",
        "60",
        "--force",
        "--dogfood",
    ]


def test_run_loop_process_recover_clears_stale_process_without_tasks(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save_process(
        loop_pid=_dead_pid(),
        child_pid=None,
        log_path=None,
        detached=False,
    )

    assert service.run_loop_process(max_tasks=1, force=True, recover=True) == 0
    assert service.state_store.load().process is None


def test_run_loop_summary_rejects_existing_doing_task(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="busy-task",
        title="Busy task",
        priority=0,
        assignee="Ralph",
        body="Already busy.",
    )
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    with pytest.raises(JriError, match="already in progress"):
        service._run_loop_summary(max_tasks=1, force=True)


def test_should_restart_process_after_iteration_branches(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    monkeypatch.delenv("JRI_ALLOW_SELF_RESTART", raising=False)

    assert not service._should_restart_process_after_iteration(
        dogfood=False,
        max_tasks=None,
        completed=0,
    )
    assert not service._should_restart_process_after_iteration(
        dogfood=True,
        max_tasks=None,
        completed=0,
    )
    monkeypatch.setenv("JRI_ALLOW_SELF_RESTART", "1")
    assert service._should_restart_process_after_iteration(
        dogfood=True,
        max_tasks=None,
        completed=0,
    )
    service.paths.stop_signal_path.parent.mkdir(parents=True, exist_ok=True)
    service.paths.stop_signal_path.write_text("stop\n", encoding="utf-8")
    assert not service._should_restart_process_after_iteration(
        dogfood=True,
        max_tasks=None,
        completed=0,
    )


def test_pi_runtime_helpers_reject_non_pi_runtime(git_repo: Path) -> None:
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    with pytest.raises(JriError, match="non-Pi"):
        service._start_pi_runtime(overrides={})
    service._stop_pi_runtime(None)


def test_cleanup_tracked_processes_clears_missing_optional_process(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    assert service._cleanup_tracked_processes(required=False) is False
    assert service.state_store.load().process is None


def test_cleanup_tracked_processes_kills_foreign_process_group(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save_process(
        loop_pid=4242,
        child_pid=4242,
        log_path=None,
        detached=True,
    )
    killpg_calls: list[int] = []
    kill_calls: list[int] = []
    monkeypatch.setattr("jri.core.service.os.getpgrp", lambda: 1)
    monkeypatch.setattr("jri.core.service.os.getpgid", lambda pid: 99)
    monkeypatch.setattr(
        "jri.core.service.os.killpg",
        lambda pgid, sig: killpg_calls.append(pgid),
    )
    monkeypatch.setattr(
        "jri.core.service.os.kill",
        lambda pid, sig: kill_calls.append(pid),
    )

    assert service._cleanup_tracked_processes(required=True) is True
    assert killpg_calls == [99]
    assert kill_calls == []


def test_cleanup_tracked_processes_falls_back_when_group_lookup_fails(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save_process(
        loop_pid=5151,
        child_pid=None,
        log_path=None,
        detached=True,
    )
    kill_calls: list[int] = []
    monkeypatch.setattr(
        "jri.core.service.os.getpgid",
        lambda pid: (_ for _ in ()).throw(PermissionError),
    )
    monkeypatch.setattr(
        "jri.core.service.os.kill",
        lambda pid, sig: kill_calls.append(pid),
    )

    assert service._cleanup_tracked_processes(required=True) is True
    assert kill_calls == [5151]


def test_dirty_workdir_prompt_allows_stash_discard_and_abort(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(service.git, "status_short", lambda *args: " M README.md")
    monkeypatch.setattr(
        service.git,
        "run",
        lambda *args, **kwargs: commands.append(args),
    )
    monkeypatch.setattr("builtins.input", lambda: "s")
    service._handle_dirty_workdir(force=False)
    assert commands[-1] == ("stash",)
    monkeypatch.setattr("builtins.input", lambda: "d")
    service._handle_dirty_workdir(force=False)
    assert commands[-2:] == [("checkout", "."), ("clean", "-fd")]
    monkeypatch.setattr("builtins.input", lambda: "a")
    with pytest.raises(JriError, match="aborted by user"):
        service._handle_dirty_workdir(force=False)


def test_wrong_branch_prompt_allows_checkout_or_abort(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(service.git, "default_branch", lambda hint=None: "main")
    monkeypatch.setattr(service.git, "current_branch", lambda: "feature")
    monkeypatch.setattr(
        service.git,
        "run",
        lambda *args, **kwargs: commands.append(args),
    )
    monkeypatch.setattr("builtins.input", lambda: "")
    service._handle_wrong_branch(force=False)
    assert commands == [("checkout", "main")]
    monkeypatch.setattr("builtins.input", lambda: "n")
    with pytest.raises(JriError, match="aborted by user"):
        service._handle_wrong_branch(force=False)


def test_stale_start_state_prompt_abort_and_started_state_cleanup(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="stale-task",
        title="Stale task",
        priority=0,
        assignee="Ralph",
        body="Stale.",
    )
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    monkeypatch.setattr("builtins.input", lambda: "n")
    with pytest.raises(JriError, match="aborted by user"):
        service._recover_stale_start_state(mode="foreground", force=False)
    (git_repo / ".jri" / "tasks" / "doing" / "stale-task.md").unlink()
    service.state_store.save(State(started_at=123))
    service._recover_stale_start_state(mode="foreground", force=True)
    assert service.state_store.load().started_at is None


def test_timeline_event_ts_filters_by_task_event_and_start_time(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    from jri.core.timeline import TimelineEvent

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.timeline.record(
        TimelineEvent(
            ts="1970-01-01T00:00:01Z",
            event="make_check_passed",
            task="a",
        )
    )
    service.timeline.record(
        TimelineEvent(
            ts="1970-01-01T00:00:03Z",
            event="make_check_passed",
            task="a",
        )
    )

    assert (
        service._timeline_event_ts(
            task_slug="a",
            event="make_check_passed",
            not_before=2,
        )
        == "1970-01-01T00:00:03Z"
    )
    assert (
        service._timeline_event_ts(
            task_slug="b",
            event="make_check_passed",
            not_before=None,
        )
        is None
    )


def test_recover_unverified_completed_attempt_logs_failure(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="done",
        slug="done-task",
        title="Done task",
        priority=0,
        assignee="Ralph",
        body="Done.",
    )
    git(git_repo, "add", ".jri/tasks/done/done-task.md")
    git(git_repo, "commit", "-m", "seed done task")
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    monkeypatch.setattr(
        "jri.core.service.move_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("move failed")),
    )

    with pytest.raises(OSError, match="move failed"):
        service._recover_unverified_completed_attempt(
            AttemptState(
                number=1,
                task_slug="done-task",
                branch="ralph",
                started_at=1,
            ),
            mode="foreground",
            reason="missing-completion-evidence",
            process=None,
        )
    assert "phase=recover-unverified-completed-attempt" in (
        git_repo / ".jri" / "logs" / "recovery-failures.log"
    ).read_text(encoding="utf-8")


def test_complete_attempt_commits_partial_work_on_ralph_branch(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="done-task",
        title="Done task",
        priority=0,
        assignee="Ralph",
        body="Done.",
    )
    git(git_repo, "add", ".jri/tasks/doing/done-task.md")
    git(git_repo, "commit", "-m", "seed doing task")
    git(git_repo, "checkout", "-b", "ralph/main")
    git(git_repo, "tag", "jri/begin/done-task")
    (git_repo / "partial.txt").write_text("partial\n", encoding="utf-8")
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    attempt = AttemptState(
        number=1,
        task_slug="done-task",
        branch="ralph/main",
        started_at=1,
        finished_at=2,
        result="completed",
    )

    service._complete_attempt(
        attempt,
        doing_task=parse_task_file(
            git_repo / ".jri" / "tasks" / "doing" / "done-task.md"
        ),
    )

    assert git(git_repo, "branch", "--show-current") == "main"
    assert (git_repo / ".jri" / "tasks" / "done" / "done-task.md").exists()
    assert service.state_store.load().active_attempt is None


def test_complete_attempt_rejects_unrelated_branch(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    git(git_repo, "checkout", "-b", "feature")
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    with pytest.raises(JriError, match="must begin from the main branch"):
        service._complete_attempt(
            AttemptState(
                number=1,
                task_slug="task-a",
                branch="ralph/main",
                started_at=1,
            ),
            doing_task=None,
        )


def test_load_attempt_history_ignores_non_list_payload(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    history_path = git_repo / ".jri" / "attempts" / "task-a.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text('{"attempts": {}}\n', encoding="utf-8")

    assert (
        JriService(
            git_repo,
            agent_runtime=SuccessfulFakeAgentRuntime(),
        )._load_attempt_history("task-a")
        == []
    )


def test_save_runtime_process_preserves_parent_log_and_detached_flag(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    parent_log = git_repo / ".jri" / "logs" / "ralph" / "parent.log"
    child_log = git_repo / ".jri" / "logs" / "ralph" / "child.log"
    service.state_store.save_process(
        loop_pid=os.getpid(),
        child_pid=None,
        log_path=parent_log,
        detached=True,
    )

    service._save_runtime_process(child_pid=123, task_log_path=child_log)

    process = service.state_store.load().process
    assert process is not None
    assert process.child_pid == 123
    assert process.log_path is not None
    assert Path(process.log_path) == parent_log
    assert process.detached is True


def test_set_tracked_process_detached_noops_without_process(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    service._set_tracked_process_detached(detached=True)

    assert service.state_store.load().process is None


def test_follow_control_monitor_handles_missing_terminal(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import termios

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    monkeypatch.setattr(
        "jri.core.service.termios.tcgetattr",
        lambda fd: (_ for _ in ()).throw(termios.error("not tty")),
    )

    with service._follow_control_monitor(enabled=True) as controls:
        assert controls.enabled is False


def test_current_follow_task_prefers_unfinished_active_attempt(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.start_attempt(
        AttemptState(number=1, task_slug="active-task", branch="ralph", started_at=1)
    )

    assert service._current_follow_task() == "active-task"


def test_create_needs_human_task_requires_structured_payload(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    task = parse_task_file(
        write_task(
            git_repo,
            status="todo",
            slug="task-a",
            title="Task A",
            priority=0,
            assignee="Ralph",
            body="Needs help.",
        )
    )
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    with pytest.raises(JriError, match="missing human_task"):
        service._create_needs_human_task(
            task,
            None,
            log_path=git_repo / ".jri" / "logs" / "ralph" / "task.log",
            session_id=None,
            export_path=None,
        )


def test_block_task_on_dependency_does_not_duplicate_existing_dependency(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    task = parse_task_file(
        write_task(
            git_repo,
            status="todo",
            slug="task-a",
            title="Task A",
            priority=0,
            assignee="Ralph",
            body="Body.",
            depends_on=["human-task"],
        )
    )
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    blocked = service._block_task_on_dependency(task, "human-task")

    assert blocked.metadata.depends_on == ["human-task"]


def test_ensure_promoted_task_pristine_reports_mutation(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    path = write_task(
        git_repo,
        status="todo",
        slug="task-a",
        title="Task A",
        priority=0,
        assignee="Ralph",
        body="Original.",
    )
    task = parse_task_file(path)
    baseline = path.read_text(encoding="utf-8")
    path.write_text(baseline + "\nmutated\n", encoding="utf-8")

    with pytest.raises(JriError, match="modified in place"):
        JriService(
            git_repo,
            agent_runtime=SuccessfulFakeAgentRuntime(),
        )._ensure_promoted_task_pristine(task, baseline=baseline)


def test_template_resource_parts_strip_managed_root() -> None:
    import jri.core.service as service_module

    assert service_module._template_resource_parts(".jri/tasks/todo/.gitkeep") == (
        "tasks",
        "todo",
        ".gitkeep",
    )
    assert service_module._single_line("a  b\n c", limit=20) == "a b c"
    assert service_module._single_line("abcdef", limit=5) == "ab..."


def test_signal_handler_sets_halt_requested(git_repo: Path) -> None:
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    handlers = service._install_signal_handlers()
    try:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        with pytest.raises(HaltRequested, match="Ralph halt requested"):
            cast(Any, handler)(signal.SIGTERM, None)
        assert service._halt_requested is True
    finally:
        service._restore_signal_handlers(handlers)


def test_start_summary_pi_runtime_uses_model_overrides(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    runtime = RefreshCapturingPiRuntime()
    service = JriService(git_repo, agent_runtime=runtime)
    captured: dict[str, object] = {}

    def fake_summary(
        max_tasks: int | None,
        **kwargs: object,
    ) -> RunSummary:
        captured["max_tasks"] = max_tasks
        captured.update(kwargs)
        return RunSummary(completed=0, outcome="no_work", task_results={})

    monkeypatch.setattr(service, "_run_loop_summary", fake_summary)

    summary = service.start_summary(
        max_tasks=2,
        model="ralph-model",
        validator_model="validator-model",
        general_model="general-model",
        explore_model="explore-model",
        task_timeout=30,
        force=True,
        dogfood=True,
    )

    assert summary.completed == 0
    assert captured["max_tasks"] == 2
    assert captured["task_timeout"] == 30
    assert captured["force"] is True
    assert captured["dogfood"] is True
    assert captured["model_overrides"] == {
        "ralph": "ralph-model",
        "ralph-validator": "validator-model",
        "general": "general-model",
        "explore": "explore-model",
    }


def test_recover_stale_start_state_resumes_doing_task_with_evidence(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    task = parse_task_file(
        write_task(
            git_repo,
            status="doing",
            slug="task-a",
            title="Task A",
            priority=0,
            assignee="Ralph",
            body="Done but interrupted.",
        )
    )
    attempt = AttemptState(
        number=1,
        task_slug="task-a",
        branch="ralph/main",
        started_at=1,
        finished_at=2,
        result="completed",
    )
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save(State(active_attempt=attempt, attempts=[attempt]))
    completed: list[tuple[AttemptState, Task | None]] = []
    monkeypatch.setattr(
        service,
        "_attempt_completion_evidence",
        lambda active_attempt: {"end_tag": "jri/end/task-a"},
    )
    monkeypatch.setattr(
        service,
        "_complete_attempt",
        lambda active_attempt, *, doing_task: completed.append(
            (active_attempt, doing_task)
        ),
    )

    service._recover_stale_start_state(mode="foreground", force=True)

    assert completed == [(attempt, task)]
    recovery_log = (git_repo / ".jri" / "logs" / "recovery.log").read_text(
        encoding="utf-8"
    )
    assert "reason=resume-completed-attempt" in recovery_log
    assert "evidence=end_tag:jri/end/task-a" in recovery_log


def test_recover_stale_start_state_handles_finished_active_attempt_without_task(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    attempt = AttemptState(
        number=1,
        task_slug="task-a",
        branch="ralph/main",
        started_at=1,
        finished_at=2,
        result="completed",
    )
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save(State(active_attempt=attempt, attempts=[attempt]))
    completed: list[Task | None] = []
    monkeypatch.setattr(
        service,
        "_attempt_completion_evidence",
        lambda active_attempt: {"end_tag": "jri/end/task-a"},
    )
    monkeypatch.setattr(
        service,
        "_complete_attempt",
        lambda active_attempt, *, doing_task: completed.append(doing_task),
    )

    service._recover_stale_start_state(mode="foreground", force=True)

    assert completed == [None]


@pytest.mark.parametrize(
    "result",
    ["failed", "incompleted", "needs_human", "interrupted"],
)
def test_recover_stale_start_state_clears_terminal_active_attempt_results(
    git_repo: Path,
    result: str,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    attempt = AttemptState(
        number=1,
        task_slug="task-a",
        branch="ralph/main",
        started_at=1,
        result=cast(Any, result),
    )
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    service.state_store.save(State(active_attempt=attempt, attempts=[attempt]))

    service._recover_stale_start_state(mode="foreground", force=True)

    assert service.state_store.load().active_attempt is None


def test_recover_stale_start_state_records_missing_loop_pid(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    state = State(
        process=ProcessState(
            loop_pid=cast(Any, None),
            child_pid=None,
            log_path=None,
            detached=False,
        )
    )
    monkeypatch.setattr(service.state_store, "load", lambda: state)
    reset_calls: list[str] = []
    monkeypatch.setattr(
        service,
        "_reset_runtime_state",
        lambda: reset_calls.append("reset"),
    )

    service._recover_stale_start_state(mode="foreground", force=True)

    assert reset_calls == ["reset"]
    assert "reason=missing-loop-pid" in (
        git_repo / ".jri" / "logs" / "recovery.log"
    ).read_text(encoding="utf-8")


def test_recover_stale_task_rejects_dirty_default_branch(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    task = parse_task_file(
        write_task(
            git_repo,
            status="doing",
            slug="stale-task",
            title="Stale task",
            priority=0,
            assignee="Ralph",
            body="Stale.",
        )
    )
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    monkeypatch.setattr(service.git, "status_short", lambda *args: " M README.md")

    with pytest.raises(JriError, match="working tree must be clean"):
        service._recover_stale_task(
            task,
            mode="foreground",
            reason="no-tracked-process",
            process=None,
        )

    assert "phase=recover-stale-task" in (
        git_repo / ".jri" / "logs" / "recovery-failures.log"
    ).read_text(encoding="utf-8")


def test_recover_stale_task_commits_partial_from_managed_branch(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    task = parse_task_file(
        write_task(
            git_repo,
            status="doing",
            slug="stale-task",
            title="Stale task",
            priority=0,
            assignee="Ralph",
            body="Stale.",
        )
    )
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(service.git, "current_branch", lambda: "ralph/main")
    monkeypatch.setattr(
        service.git,
        "commit_all_if_needed",
        lambda message: calls.append(("commit", message)),
    )
    monkeypatch.setattr(
        service.git,
        "checkout",
        lambda branch: calls.append(("checkout", branch)),
    )
    monkeypatch.setattr(
        service,
        "_reset_runtime_state",
        lambda: calls.append(("reset", None)),
    )

    service._recover_stale_task(
        task,
        mode="foreground",
        reason="no-tracked-process",
        process=None,
    )

    assert ("checkout", "main") in calls
    assert (git_repo / ".jri" / "tasks" / "todo" / "stale-task.md").exists()


def test_recover_stale_task_rejects_unrelated_branch(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    task = parse_task_file(
        write_task(
            git_repo,
            status="doing",
            slug="stale-task",
            title="Stale task",
            priority=0,
            assignee="Ralph",
            body="Stale.",
        )
    )
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    monkeypatch.setattr(service.git, "current_branch", lambda: "feature")

    with pytest.raises(JriError, match="must begin from the main branch"):
        service._recover_stale_task(
            task,
            mode="foreground",
            reason="no-tracked-process",
            process=None,
        )


def test_recover_unverified_completed_attempt_rejects_unrelated_branch(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    monkeypatch.setattr(service.git, "current_branch", lambda: "feature")

    with pytest.raises(JriError, match="must begin from the main branch"):
        service._recover_unverified_completed_attempt(
            AttemptState(number=1, task_slug="task-a", branch="ralph", started_at=1),
            mode="foreground",
            reason="missing-completion-evidence",
            process=None,
        )


def test_follow_log_cancels_stop_and_detaches_after_log_exists(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    log_path = git_repo / ".jri" / "logs" / "ralph" / "running.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("running\n", encoding="utf-8")
    service.stop("already requested")

    class FakeControls:
        stop_requested = True
        confirming_halt = False
        halt_armed = False
        _actions = iter(["stop_cancel", "detach"])

        def poll_action(self) -> str | None:
            return next(self._actions, None)

    @contextmanager
    def fake_monitor(*, enabled: bool) -> Iterator[FakeControls]:
        assert enabled is True
        yield FakeControls()

    monkeypatch.setattr("jri.core.service.supports_interactive_footer", lambda: True)
    monkeypatch.setattr(service, "_follow_control_monitor", fake_monitor)

    assert service._follow_log(log_path, loop_pid=12345, allow_detach=True) is True
    assert not service.paths.stop_signal_path.exists()
    assert (
        "Detached. Use `jri attach` to follow the run again." in capsys.readouterr().out
    )


def test_validate_promotion_approval_rejects_mismatched_and_changed_drafts(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    path = write_task(
        git_repo,
        status="draft",
        slug="draft-a",
        title="Draft A",
        priority=0,
        assignee="Ralph",
        body="Draft body.",
    )
    task = parse_task_file(path)
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    digest = service._draft_content_digests([task])["draft-a"]
    service.state_store.save_promotion(
        PromotionRecord(
            confirmed_at=1,
            task_slugs=["other-draft"],
            content_digests={"draft-a": digest},
        )
    )

    with pytest.raises(JriError, match="must match"):
        service._validate_promotion_approval([task])

    service.state_store.save_promotion(
        PromotionRecord(
            confirmed_at=1,
            task_slugs=["draft-a"],
            content_digests={"draft-a": "0" * 64},
        )
    )
    with pytest.raises(JriError, match="changed since approval"):
        service._validate_promotion_approval([task])
    assert service.state_store.load().promotion is None


def test_reset_tag_helpers_cover_unmatched_and_unparsed_tags(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    monkeypatch.setattr(
        service.git,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            stdout="not-a-jri-tag\n",
            stderr="",
        ),
    )
    assert service._find_latest_tag("end") is None

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    git(git_repo, "tag", "plain-tag")
    assert service._resolve_reset_target_ref("plain-tag") == git(
        git_repo,
        "rev-parse",
        "plain-tag",
    )
    assert service._describe_reset_target("plain-tag") == "plain-tag"
    assert service._describe_reset_target("jri/begin/task-a") == (
        "just before jri/begin/task-a"
    )
    assert service._describe_reset_target("jri/end/task-a") == "jri/end/task-a"


def test_ensure_initialized_rejects_uninitialized_repo(git_repo: Path) -> None:
    with pytest.raises(JriError, match="project is not initialized"):
        JriService(
            git_repo, agent_runtime=SuccessfulFakeAgentRuntime()
        ).ensure_initialized()


def test_list_tasks_wraps_malformed_task_errors(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    bad_task = git_repo / ".jri" / "tasks" / "draft" / "bad.md"
    bad_task.write_text("bad\n", encoding="utf-8")

    with pytest.raises(JriError, match="malformed task file"):
        JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())._list_tasks(
            "draft"
        )


def test_start_detached_builds_optional_timeout_args(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    commands: list[list[str]] = []

    def fake_popen(*args: object, **kwargs: object) -> FakeDetachedProcess:
        commands.append(cast(list[str], args[0]))
        return FakeDetachedProcess(9090)

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    monkeypatch.setattr("jri.core.service.subprocess.Popen", fake_popen)

    assert (
        service._start_detached(
            2,
            "ralph-model",
            "validator-model",
            "general-model",
            "explore-model",
            45,
            True,
        )
        == 0
    )
    assert "--task-timeout" in commands[0]
    assert "45" in commands[0]
    assert "--dogfood" in commands[0]


def test_run_loop_summary_honors_pending_halt(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())

    def fake_install_handlers() -> dict[signal.Signals, Any]:
        service._halt_requested = True
        return {}

    monkeypatch.setattr(service, "_install_signal_handlers", fake_install_handlers)

    with pytest.raises(HaltRequested, match="Ralph halt requested"):
        service._run_loop_summary(max_tasks=1, force=True)


def test_run_loop_summary_records_timeout_result_without_running_agent(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="timeout-task",
        title="Timeout task",
        priority=0,
        assignee="Ralph",
        body="Timeout.",
    )
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    monkeypatch.setattr(service, "_run_task", lambda task, task_timeout=None: "timeout")

    summary = service._run_loop_summary(max_tasks=1, task_timeout=5, force=True)

    assert summary.completed == 0
    assert summary.outcome == "timeout"
    assert summary.task_results == {"timeout-task": "timeout"}


def test_follow_control_monitor_enables_and_restores_terminal(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import termios

    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    restored: list[tuple[int, int, list[str]]] = []
    monkeypatch.setattr("sys.stdin.fileno", lambda: 42)
    monkeypatch.setattr("jri.core.service.termios.tcgetattr", lambda fd: ["old"])
    monkeypatch.setattr("jri.core.service.tty.setcbreak", lambda fd: None)
    monkeypatch.setattr(
        "jri.core.service.termios.tcsetattr",
        lambda fd, when, previous: restored.append((fd, when, previous)),
    )

    with service._follow_control_monitor(enabled=True) as controls:
        assert controls.enabled is True
        assert controls.fd == 42

    assert restored == [(42, termios.TCSADRAIN, ["old"])]


def test_run_loop_summary_wraps_initial_task_list_errors(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    import jri.core.service as service_module

    monkeypatch.setattr(
        service_module,
        "list_tasks",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad tasks")),
    )

    with pytest.raises(JriError, match="bad tasks"):
        JriService(
            git_repo,
            agent_runtime=SuccessfulFakeAgentRuntime(),
        )._run_loop_summary(max_tasks=1, force=True)


def test_run_loop_summary_raises_restart_after_completed_iteration(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="restart-task",
        title="Restart task",
        priority=0,
        assignee="Ralph",
        body="Triggers restart.",
    )
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    monkeypatch.setattr(
        service, "_run_task", lambda task, task_timeout=None: "completed"
    )
    monkeypatch.setattr(
        service,
        "_should_restart_process_after_iteration",
        lambda **kwargs: True,
    )

    with pytest.raises(RestartRequested) as exc_info:
        service._run_loop_summary(max_tasks=2, force=True, dogfood=True)

    assert exc_info.value.remaining_tasks == 1


def test_follow_log_returns_when_log_never_appears_and_process_is_dead(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    log_path = git_repo / ".jri" / "logs" / "ralph" / "missing.log"
    monkeypatch.setattr(service, "_is_pid_alive", lambda pid: False)

    assert service._follow_log(log_path, loop_pid=12345, allow_detach=False) is False


def test_save_runtime_process_uses_child_log_when_parent_has_no_log(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    child_log = git_repo / ".jri" / "logs" / "ralph" / "child.log"
    service.state_store.save_process(
        loop_pid=os.getpid(),
        child_pid=None,
        log_path=None,
        detached=False,
    )

    service._save_runtime_process(child_pid=123, task_log_path=child_log)

    process = service.state_store.load().process
    assert process is not None
    assert Path(process.log_path or "") == child_log


def test_is_pid_alive_handles_invalid_and_permission_denied(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    assert service._is_pid_alive(0) is False
    monkeypatch.setattr(
        "jri.core.service.os.kill",
        lambda pid, sig: (_ for _ in ()).throw(PermissionError),
    )
    assert service._is_pid_alive(12345) is True


def test_recover_unverified_completed_attempt_handles_managed_branch(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, agent_runtime=SuccessfulFakeAgentRuntime())
    calls: list[str] = []
    monkeypatch.setattr(service.git, "current_branch", lambda: "ralph/main")
    monkeypatch.setattr(
        service.git, "commit_all_if_needed", lambda message: calls.append(message)
    )
    monkeypatch.setattr(service.git, "checkout", lambda branch: calls.append(branch))
    monkeypatch.setattr(service, "_reset_runtime_state", lambda: calls.append("reset"))

    service._recover_unverified_completed_attempt(
        AttemptState(number=1, task_slug="task-a", branch="ralph/main", started_at=1),
        mode="foreground",
        reason="missing-completion-evidence",
        process=None,
    )

    assert "main" in calls
    assert "reset" in calls


def test_allocate_needs_human_slug_skips_existing_suffixes(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    for slug in ("task-a--needs-human", "task-a--needs-human-2"):
        write_task(
            git_repo,
            status="todo",
            slug=slug,
            title=slug,
            priority=0,
            assignee="Human",
            body="Existing human task.",
        )

    assert (
        JriService(
            git_repo,
            agent_runtime=SuccessfulFakeAgentRuntime(),
        )._allocate_needs_human_slug("task-a")
        == "task-a--needs-human-3"
    )
