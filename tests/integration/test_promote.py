from pathlib import Path
from typing import cast

import pytest

import jri.core.git as git_module
from jri.core.service import JriService
from tests.conftest import run_cli
from tests.helpers import git, read_json, write_task


def _init(repo: Path) -> JriService:
    assert run_cli(["init"], cwd=repo) == 0
    return JriService(repo)


def test_promote_is_not_a_public_cli_command(git_repo: Path) -> None:
    _init(git_repo)

    with pytest.raises(SystemExit) as exc_info:
        run_cli(["promote", "clarify-scope"], cwd=git_repo)

    assert exc_info.value.code == 2


def test_promote_moves_selected_drafts_and_records_confirmation(git_repo: Path) -> None:
    service = _init(git_repo)
    write_task(
        git_repo,
        status="draft",
        slug="clarify-scope",
        title="Clarify scope",
        priority=0,
        assignee="Ralph",
        body="Clarify the scope.\n",
        acceptance_criteria=["The scope is written down."],
    )
    write_task(
        git_repo,
        status="draft",
        slug="build-ui",
        title="Build UI",
        priority=1,
        assignee="Ralph",
        body="Build the UI.\n",
        depends_on=["clarify-scope"],
        acceptance_criteria=["The UI is implemented."],
    )

    approved = service.approve_draft_promotion(slugs=["clarify-scope", "build-ui"])
    promoted = service.promote_drafts(slugs=["clarify-scope", "build-ui"])

    assert [task.slug for task in approved] == ["build-ui", "clarify-scope"]
    assert [task.slug for task in promoted] == ["build-ui", "clarify-scope"]
    assert not (git_repo / ".jri" / "tasks" / "draft" / "clarify-scope.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "draft" / "build-ui.md").exists()
    assert (git_repo / ".jri" / "tasks" / "todo" / "clarify-scope.md").exists()
    assert (git_repo / ".jri" / "tasks" / "todo" / "build-ui.md").exists()

    state = read_json(git_repo / ".jri" / "state.json")
    assert "promotion" not in state
    assert git_module.MSG_PROMOTE in git(
        git_repo,
        "log",
        "-1",
        "--format=%s",
    )


def test_promote_rejects_dependency_on_unselected_draft(git_repo: Path) -> None:
    service = _init(git_repo)
    write_task(
        git_repo,
        status="draft",
        slug="clarify-scope",
        title="Clarify scope",
        priority=0,
        assignee="Ralph",
        body="Clarify the scope.\n",
        acceptance_criteria=["The scope is written down."],
    )
    write_task(
        git_repo,
        status="draft",
        slug="build-ui",
        title="Build UI",
        priority=1,
        assignee="Ralph",
        body="Build the UI.\n",
        depends_on=["clarify-scope"],
        acceptance_criteria=["The UI is implemented."],
    )

    with pytest.raises(Exception, match="outside the promotion batch"):
        service.promote_drafts(slugs=["build-ui"])


def test_promote_rejects_without_validator_approval(git_repo: Path) -> None:
    service = _init(git_repo)
    write_task(
        git_repo,
        status="draft",
        slug="clarify-scope",
        title="Clarify scope",
        priority=0,
        assignee="Ralph",
        body="Clarify the scope.\n",
        acceptance_criteria=["The scope is written down."],
    )

    with pytest.raises(Exception, match="approved"):
        service.promote_drafts(slugs=["clarify-scope"])


def test_approve_draft_promotion_records_content_digests(git_repo: Path) -> None:
    service = _init(git_repo)
    write_task(
        git_repo,
        status="draft",
        slug="clarify-scope",
        title="Clarify scope",
        priority=0,
        assignee="Ralph",
        body="Clarify the scope.\n",
        acceptance_criteria=["The scope is written down."],
    )

    selected = service.approve_draft_promotion(slugs=["clarify-scope"])

    assert [task.slug for task in selected] == ["clarify-scope"]
    state = read_json(git_repo / ".jri" / "state.json")
    promotion = cast(dict[str, object], state["promotion"])
    assert promotion["task_slugs"] == ["clarify-scope"]
    digests = cast(dict[str, object], promotion["content_digests"])
    assert list(digests) == ["clarify-scope"]
    assert isinstance(digests["clarify-scope"], str)


def test_promote_rejects_when_approved_draft_changes(git_repo: Path) -> None:
    service = _init(git_repo)
    draft_path = write_task(
        git_repo,
        status="draft",
        slug="clarify-scope",
        title="Clarify scope",
        priority=0,
        assignee="Ralph",
        body="Clarify the scope.\n",
        acceptance_criteria=["The scope is written down."],
    )
    service.approve_draft_promotion(slugs=["clarify-scope"])
    draft_path.write_text(
        draft_path.read_text(encoding="utf-8") + "\nChanged after approval.\n",
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="changed since approval"):
        service.promote_drafts(slugs=["clarify-scope"])

    state = read_json(git_repo / ".jri" / "state.json")
    assert "promotion" not in state


def test_promote_check_validates_without_moving_tasks(git_repo: Path) -> None:
    service = _init(git_repo)
    write_task(
        git_repo,
        status="draft",
        slug="clarify-scope",
        title="Clarify scope",
        priority=0,
        assignee="Ralph",
        body="Clarify the scope.\n",
        acceptance_criteria=["The scope is written down."],
    )
    head_before = git(git_repo, "rev-parse", "HEAD")

    selected = service.check_draft_promotion(slugs=["clarify-scope"])

    assert [task.slug for task in selected] == ["clarify-scope"]
    assert (git_repo / ".jri" / "tasks" / "draft" / "clarify-scope.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "todo" / "clarify-scope.md").exists()
    assert git(git_repo, "status", "--short") == ""
    assert git(git_repo, "rev-parse", "HEAD") != head_before
    assert git_module.MSG_CHECK_PROMOTE == git(
        git_repo,
        "log",
        "-1",
        "--format=%s",
    )


def test_promote_check_reports_validation_failures(git_repo: Path) -> None:
    service = _init(git_repo)
    write_task(
        git_repo,
        status="draft",
        slug="clarify-scope",
        title="Clarify scope",
        priority=0,
        assignee="Ralph",
        body="Clarify the scope.\n",
        acceptance_criteria=["The scope is written down."],
    )
    write_task(
        git_repo,
        status="draft",
        slug="build-ui",
        title="Build UI",
        priority=1,
        assignee="Ralph",
        body="Build the UI.\n",
        depends_on=["clarify-scope"],
        acceptance_criteria=["The UI is implemented."],
    )

    head_before = git(git_repo, "rev-parse", "HEAD")

    with pytest.raises(Exception, match="outside the promotion batch"):
        service.check_draft_promotion(slugs=["build-ui"])

    assert (git_repo / ".jri" / "tasks" / "draft" / "build-ui.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "todo" / "build-ui.md").exists()
    assert git(git_repo, "rev-parse", "HEAD") == head_before


def test_promote_rejects_cyclic_dependencies(git_repo: Path) -> None:
    service = _init(git_repo)
    write_task(
        git_repo,
        status="draft",
        slug="alpha",
        title="Alpha",
        priority=1,
        assignee="Ralph",
        body="Alpha task.\n",
        depends_on=["beta"],
        acceptance_criteria=["Alpha done."],
    )
    write_task(
        git_repo,
        status="draft",
        slug="beta",
        title="Beta",
        priority=1,
        assignee="Ralph",
        body="Beta task.\n",
        depends_on=["alpha"],
        acceptance_criteria=["Beta done."],
    )

    with pytest.raises(Exception, match="cyclic dependency"):
        service.promote_drafts(slugs=["alpha", "beta"])
