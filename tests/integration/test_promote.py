from pathlib import Path
from typing import cast

from tests.conftest import run_cli
from tests.helpers import git, read_json, write_task


def _init(repo: Path) -> None:
    assert run_cli(["init"], cwd=repo) == 0


def test_promote_aborts_on_no_response(git_repo: Path, capsys, monkeypatch) -> None:
    _init(git_repo)
    write_task(
        git_repo,
        status="draft",
        slug="clarify-scope",
        title="Clarify scope",
        priority=1,
        assignee="Ralph",
        body="Clarify the scope.\n",
        acceptance_criteria=["The scope is written down."],
    )

    monkeypatch.setattr("builtins.input", lambda: "n")
    rc = run_cli(["promote", "clarify-scope"], cwd=git_repo)

    assert rc == 1
    assert "Promotion aborted." in capsys.readouterr().err
    assert (git_repo / ".jri" / "tasks" / "draft" / "clarify-scope.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "todo" / "clarify-scope.md").exists()


def test_promote_moves_selected_drafts_and_records_confirmation(
    git_repo: Path, capsys
) -> None:
    _init(git_repo)
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

    rc = run_cli(
        [
            "promote",
            "clarify-scope",
            "build-ui",
            "--force",
        ],
        cwd=git_repo,
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "Promoted 2 draft task(s) to todo." in out
    assert "clarify-scope" in out
    assert "build-ui" in out
    assert not (git_repo / ".jri" / "tasks" / "draft" / "clarify-scope.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "draft" / "build-ui.md").exists()
    assert (git_repo / ".jri" / "tasks" / "todo" / "clarify-scope.md").exists()
    assert (git_repo / ".jri" / "tasks" / "todo" / "build-ui.md").exists()

    state = read_json(git_repo / ".jri" / "state.json")
    promotion = cast(dict[str, object], state["promotion"])
    assert promotion == {
        "confirmed_at": promotion["confirmed_at"],
        "task_slugs": ["build-ui", "clarify-scope"],
        "target_status": "todo",
    }
    assert "jri promote: move drafts to todo" in git(
        git_repo,
        "log",
        "-1",
        "--format=%s",
    )


def test_promote_rejects_dependency_on_unselected_draft(git_repo: Path, capsys) -> None:
    _init(git_repo)
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

    rc = run_cli(
        ["promote", "build-ui", "--force"],
        cwd=git_repo,
    )

    assert rc == 1
    assert "outside the promotion batch" in capsys.readouterr().err


def test_promote_rejects_cyclic_dependencies(git_repo: Path, capsys) -> None:
    _init(git_repo)
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

    rc = run_cli(
        ["promote", "alpha", "beta", "--force"],
        cwd=git_repo,
    )

    assert rc == 1
    assert "cyclic dependency" in capsys.readouterr().err
