from pathlib import Path
from typing import Literal, cast

import pytest

from jri.core.models import CompilerTaskSpec
from jri.core.tasks import create_task_batch, parse_task_file
from tests.helpers import write_task


def spec(
    title: str,
    *,
    priority: int = 1,
    assignee: str = "Ralph",
    depends_on: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    body: str = "Complete the task.\n",
) -> CompilerTaskSpec:
    criteria = (
        ["The behavior is verified"]
        if acceptance_criteria is None
        else acceptance_criteria
    )
    return CompilerTaskSpec(
        title=title,
        priority=priority,
        assignee=cast(Literal["Ralph", "Human"], assignee),
        depends_on=depends_on or [],
        acceptance_criteria=criteria,
        body=body,
    )


def task_path(repo: Path, slug: str) -> Path:
    return repo / ".jri" / "tasks" / "todo" / f"{slug}.md"


def test_create_task_batch_writes_deterministic_todo_tasks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".jri" / "tasks" / "done").mkdir(parents=True)
    write_task(
        repo,
        status="done",
        slug="design-ready",
        title="Design ready",
        priority=1,
        assignee="Ralph",
        body="Existing dependency.\n",
    )

    tasks = create_task_batch(
        repo,
        [
            spec("Build compiler writer", depends_on=["design-ready"]),
            spec(
                "Wire compiler output",
                priority=2,
                depends_on=["build-compiler-writer"],
            ),
        ],
    )

    assert [task.slug for task in tasks] == [
        "build-compiler-writer",
        "wire-compiler-output",
    ]
    first = parse_task_file(task_path(repo, "build-compiler-writer"))
    second = parse_task_file(task_path(repo, "wire-compiler-output"))
    assert first.metadata.depends_on == ["design-ready"]
    assert first.metadata.acceptance_criteria == ["The behavior is verified"]
    assert first.body == "Complete the task.\n"
    assert second.metadata.depends_on == ["build-compiler-writer"]
    assert second.metadata.priority == 2


def test_create_task_batch_rejects_duplicate_derived_slugs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"

    with pytest.raises(ValueError, match="duplicate task slug `build-api`"):
        create_task_batch(repo, [spec("Build API"), spec("Build API!")])

    assert not (repo / ".jri").exists()


def test_create_task_batch_rejects_invalid_dependency(tmp_path: Path) -> None:
    repo = tmp_path / "repo"

    with pytest.raises(ValueError, match="unknown dependency `missing-task`"):
        create_task_batch(repo, [spec("Build API", depends_on=["missing-task"])])

    assert not task_path(repo, "build-api").exists()


def test_create_task_batch_rejects_invalid_priority(tmp_path: Path) -> None:
    repo = tmp_path / "repo"

    with pytest.raises(ValueError, match="priority"):
        create_task_batch(repo, [spec("Build API", priority=9)])

    assert not task_path(repo, "build-api").exists()


def test_create_task_batch_rejects_missing_acceptance_criteria(tmp_path: Path) -> None:
    repo = tmp_path / "repo"

    with pytest.raises(ValueError, match="acceptance_criteria"):
        create_task_batch(repo, [spec("Build API", acceptance_criteria=[])])

    assert not task_path(repo, "build-api").exists()


def test_create_task_batch_rejects_existing_promoted_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".jri" / "tasks" / "doing").mkdir(parents=True)
    existing = write_task(
        repo,
        status="doing",
        slug="build-api",
        title="Build API",
        priority=1,
        assignee="Ralph",
        body="Existing task.\n",
    )
    original = existing.read_text(encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="refusing to overwrite existing task `build-api`",
    ):
        create_task_batch(repo, [spec("Build API")])

    assert existing.read_text(encoding="utf-8") == original
    assert not task_path(repo, "build-api").exists()


def test_create_task_batch_rejects_path_escape_slug(tmp_path: Path) -> None:
    repo = tmp_path / "repo"

    with pytest.raises(ValueError, match="could not derive a valid slug"):
        create_task_batch(repo, [spec("!!!")])

    assert not (repo / ".jri").exists()


def test_create_task_batch_rolls_back_files_on_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    original_write_text = Path.write_text

    def fail_second_write(
        self: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if self.name == "second-task.md":
            raise OSError("disk full")
        return original_write_text(
            self,
            data,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(Path, "write_text", fail_second_write)

    with pytest.raises(OSError, match="disk full"):
        create_task_batch(repo, [spec("First task"), spec("Second task")])

    assert not task_path(repo, "first-task").exists()
    assert not task_path(repo, "second-task").exists()
