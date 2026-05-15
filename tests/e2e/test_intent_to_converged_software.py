import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from jri.core.tasks import list_tasks
from tests.helpers import git

pytestmark = [pytest.mark.e2e, pytest.mark.live]

_CHAT_TIMEOUT_SECONDS = 900
_START_TASK_TIMEOUT_SECONDS = 900
_START_PROCESS_TIMEOUT_SECONDS = 2400


def test_e2e_intent_to_converged_software(
    git_repo: Path,
    run_live_agent: bool,
    preset: str | None,
    tmp_path: Path,
) -> None:
    if not run_live_agent:
        pytest.skip("pass -L/--run-live-agent to enable live agent E2E tests")

    env = _isolated_live_env(tmp_path)
    _run(["jri", "init"], cwd=git_repo, env=env)

    chat = _run_with_heartbeat(
        [
            "jri",
            "chat",
            "--fresh",
            *_preset_args(preset),
            "--print",
            "--no-session",
            "--tools",
            "read,ls,grep,find,upsert-task,read-tasks",
            _markit_intent_script(),
        ],
        cwd=git_repo,
        env=env,
        label="intent chat",
        timeout=_CHAT_TIMEOUT_SECONDS,
    )

    todo_tasks = list_tasks(git_repo / ".jri" / "tasks" / "todo")
    assert len(todo_tasks) == 2, f"expected 2 todo tasks after {chat.args}"
    for task in todo_tasks:
        assert task.metadata.assignee == "Ralph"
        assert task.metadata.acceptance_criteria
        for criterion in task.metadata.acceptance_criteria:
            assert _is_concrete_acceptance_criterion(criterion), criterion
    slugs = sorted(task.slug for task in todo_tasks)

    _run_with_heartbeat(
        [
            "jri",
            "start",
            *_preset_args(preset),
            "--tasks",
            str(len(todo_tasks)),
            "--task-timeout",
            str(_START_TASK_TIMEOUT_SECONDS),
            "--force",
        ],
        cwd=git_repo,
        env=env,
        label="jri start",
        timeout=_START_PROCESS_TIMEOUT_SECONDS,
    )

    _assert_markit_behavior(git_repo, env=env)
    _run(["make", "check"], cwd=git_repo, env=env)
    _assert_jri_run_artifacts(git_repo, slugs=slugs, env=env)
    assert git(git_repo, "status", "--short", "--untracked-files=all") == ""


def _run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, (
        f"command failed: {' '.join(args)}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return result


def _run_with_heartbeat(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    label: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    while True:
        try:
            returncode = process.wait(timeout=30)
            break
        except subprocess.TimeoutExpired as exc:
            elapsed = int(timeout - max(0, deadline - time.monotonic()))
            print(f"{label}: still running after {elapsed}s", flush=True)
            if time.monotonic() <= deadline:
                continue
            _terminate_process_group(process)
            raise AssertionError(
                f"command timed out after {timeout}s: {_command_label(args)}"
            ) from exc

    assert returncode == 0, (
        f"command failed with status {returncode}: {_command_label(args)}"
    )
    return subprocess.CompletedProcess(args=args, returncode=returncode)


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, 15)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, 9)
        process.wait(timeout=5)


def _command_label(args: list[str]) -> str:
    if args[:2] == ["jri", "chat"]:
        return "jri chat ..."
    return " ".join(args)


def _isolated_live_env(_tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    path = env.get("PATH", "")
    scripts_dir = Path(sys.executable).resolve().parent
    env["PATH"] = f"{scripts_dir}{os.pathsep}{path}" if path else str(scripts_dir)
    return env


def _preset_args(preset: str | None) -> list[str]:
    return ["--preset", preset] if preset else []


def _markit_intent_script() -> str:
    return """
Raw intent:
Build a local personal bookmark CLI called `markit`. I want to save links with
titles and tags, search them later, and archive old ones.

Use this scripted answer sheet instead of asking follow-up questions:
- Build a Python standard-library command line app runnable as `markit` when
  the repository root is on PATH.
- Store runtime bookmark data in `.markit/bookmarks.json`.
- Add `.markit/` and Python cache artifacts to `.gitignore` so normal use of
  the CLI does not dirty `git status`.
- The command `markit add URL TITLE --tag TAG` appends an active
  bookmark, allocates integer IDs starting at 1, and prints exactly
  `added {id} {title} {url}`.
- `markit list` prints one active bookmark per line as
  `{id} [active] {title} {url} tags={comma-separated-tags}`.
- `markit search TERM` is case-insensitive across title, URL, and tags, and
  hides archived bookmarks.
- `markit archive ID` marks a bookmark archived and prints exactly
  `archived {id}`.
- `markit list --all` includes archived bookmarks with `[archived]`; default
  list and search hide archived bookmarks.
- Missing or unknown IDs should exit nonzero and print a clear stderr message.
- `make check` must pass and exercise the markit CLI behavior.
- Do not create docs or prompt tests.

Create exactly two todo tasks assigned to Ralph:
1. Implement add/list persistence and the quality entrypoint.
2. Implement search/archive/list --all, depending on the first task.

Each todo task must include concrete, observable acceptance criteria with exact
command names, files, output strings, and pass/fail conditions. Stop after the
todo tasks exist. Do not run the implementation.
""".strip()


def _is_concrete_acceptance_criterion(criterion: str) -> bool:
    lowered = criterion.lower()
    vague_terms = ("as needed", "appropriate", "clean up", "improve")
    concrete_signals = (
        "`",
        "markit",
        "make check",
        "exactly",
        "nonzero",
        "git status",
        "no documentation",
        "no prompt",
    )
    return (
        len(criterion.strip()) >= 20
        and any(signal in lowered for signal in concrete_signals)
        and not any(term in lowered for term in vague_terms)
    )


def _assert_markit_behavior(repo: Path, *, env: dict[str, str]) -> None:
    markit = repo / "markit"
    assert markit.is_file()
    markit_env = {**env, "PATH": f"{repo}{os.pathsep}{env.get('PATH', '')}"}

    add_example = _run(
        [
            "markit",
            "add",
            "https://example.com",
            "Example",
            "--tag",
            "docs",
        ],
        cwd=repo,
        env=markit_env,
    )
    assert add_example.stdout.strip() == "added 1 Example https://example.com"

    add_python = _run(
        [
            "markit",
            "add",
            "https://python.org",
            "Python",
            "--tag",
            "language",
        ],
        cwd=repo,
        env=markit_env,
    )
    assert add_python.stdout.strip() == "added 2 Python https://python.org"

    listed = _run(["markit", "list"], cwd=repo, env=markit_env).stdout.splitlines()
    assert listed == [
        "1 [active] Example https://example.com tags=docs",
        "2 [active] Python https://python.org tags=language",
    ]

    assert _run(
        ["markit", "search", "docs"], cwd=repo, env=markit_env
    ).stdout.splitlines() == ["1 [active] Example https://example.com tags=docs"]
    assert _run(
        ["markit", "search", "example"], cwd=repo, env=markit_env
    ).stdout.splitlines() == ["1 [active] Example https://example.com tags=docs"]
    assert _run(
        ["markit", "search", "DoCs"], cwd=repo, env=markit_env
    ).stdout.splitlines() == ["1 [active] Example https://example.com tags=docs"]
    assert _run(
        ["markit", "search", "PY"], cwd=repo, env=markit_env
    ).stdout.splitlines() == ["2 [active] Python https://python.org tags=language"]

    archived = _run(["markit", "archive", "1"], cwd=repo, env=markit_env)
    assert archived.stdout.strip() == "archived 1"

    assert _run(["markit", "list"], cwd=repo, env=markit_env).stdout.splitlines() == [
        "2 [active] Python https://python.org tags=language"
    ]
    assert _run(["markit", "search", "example"], cwd=repo, env=markit_env).stdout == ""
    assert _run(
        ["markit", "list", "--all"], cwd=repo, env=markit_env
    ).stdout.splitlines() == [
        "1 [archived] Example https://example.com tags=docs",
        "2 [active] Python https://python.org tags=language",
    ]

    missing = subprocess.run(
        ["markit", "archive", "999"],
        cwd=repo,
        env=markit_env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing.returncode != 0
    assert "999" in missing.stderr


def _assert_jri_run_artifacts(
    repo: Path,
    *,
    slugs: list[str],
    env: dict[str, str],
) -> None:
    status = _run(["jri", "status"], cwd=repo, env=env).stdout
    assert "done" in status
    assert "todo" in status
    assert list_tasks(repo / ".jri" / "tasks" / "doing") == []
    assert [
        task
        for task in list_tasks(repo / ".jri" / "tasks" / "todo")
        if task.metadata.assignee == "Ralph"
    ] == []
    tags = set(git(repo, "tag").splitlines())

    for slug in slugs:
        assert f"jri/begin/{slug}" in tags
        assert f"jri/end/{slug}" in tags
        assert (repo / ".jri" / "tasks" / "done" / f"{slug}.md").exists()
        assert not (repo / ".jri" / "tasks" / "todo" / f"{slug}.md").exists()
        assert not (repo / ".jri" / "tasks" / "doing" / f"{slug}.md").exists()
        assert (
            (repo / ".jri" / "logs" / "diffs" / f"{slug}.diff")
            .read_text(encoding="utf-8")
            .strip()
        )
        assert (repo / ".jri" / "attempts" / f"{slug}.yaml").exists()
        assert not (repo / ".jri" / "attempts" / f"{slug}.json").exists()
        inspected = _run(["jri", "inspect", slug], cwd=repo, env=env).stdout
        assert slug in inspected

    state = _read_json(repo / ".jri" / "state.json")
    attempts = cast(list[dict[str, Any]], state["attempts"])
    assert [attempt["task_slug"] for attempt in attempts] == slugs
    assert [attempt["result"] for attempt in attempts] == ["completed"] * len(slugs)
    for attempt in attempts:
        log_path = Path(cast(str, attempt["log_path"]))
        assert log_path.exists()
        assert log_path.read_text(encoding="utf-8").strip()

    timeline_json = _run(["jri", "timeline", "--json"], cwd=repo, env=env).stdout
    events = [json.loads(line) for line in timeline_json.splitlines() if line.strip()]
    for slug in slugs:
        task_events = {event["event"] for event in events if event.get("task") == slug}
        assert {"attempt_started", "make_check_passed", "task_completed"} <= task_events


def _read_json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
