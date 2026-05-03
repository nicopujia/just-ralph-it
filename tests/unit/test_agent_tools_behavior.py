import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jri.core.agents.bundle._shared import tools
from jri.core.models import Task, TaskMetadata


def invoke_tool(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    payload: object,
) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return tools.main([tool_name])


def test_tools_package_exports_only_public_handlers() -> None:
    assert tools.__all__ == [
        "run_approve_draft_promotion",
        "run_contrast_check",
        "run_delete_task",
        "run_edit_draft_task",
        "run_edit_readme",
        "run_list_tasks",
        "run_promote_tasks",
        "run_ralph_result",
        "run_read_readme",
        "run_read_tasks",
        "run_rename_task",
        "run_upsert_task",
    ]


def task_payload(
    title: str = "Build safe tool tests",
    body: str = "Implement meaningful coverage.",
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "title": title,
        "body": body,
        "assignee": "Ralph",
        "priority": 1,
        "depends_on": [],
        "acceptance_criteria": ["behavior is covered"],
    }
    payload.update(overrides)
    return payload


def write_draft_task(
    repo: Path,
    slug: str,
    *,
    body: str = "Original body.",
    depends_on: list[str] | None = None,
) -> Path:
    task_path = repo / ".jri" / "tasks" / "draft" / f"{slug}.md"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "title": slug.replace("-", " ").title(),
        "priority": 1,
        "assignee": "Ralph",
        "depends_on": depends_on or [],
        "acceptance_criteria": ["done is observable"],
    }
    task_path.write_text(
        "---\n" + json.dumps(metadata, indent=2) + "\n---\n\n" + body,
        encoding="utf-8",
    )
    return task_path


def make_task(slug: str, status: str = "draft") -> Task:
    return Task(
        path=Path(".jri") / "tasks" / status / f"{slug}.md",
        slug=slug,
        metadata=TaskMetadata(
            title=slug.replace("-", " ").title(),
            priority=1,
            assignee="Ralph",
            depends_on=[],
            acceptance_criteria=["done is observable"],
        ),
        body="body",
    )


def test_dispatcher_rejects_unknown_tool(capsys: pytest.CaptureFixture[str]) -> None:
    assert tools.main(["unknown-tool"]) == 2

    captured = capsys.readouterr()
    assert "expected one tool name" in captured.err
    assert "upsert-task" in captured.err


def test_dispatcher_rejects_invalid_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{"))

    assert tools.main(["ralph-result"]) == 1

    captured = capsys.readouterr()
    assert "invalid JSON payload" in captured.err


def test_dispatcher_prints_successful_tool_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        invoke_tool(
            monkeypatch,
            "check-contrast",
            {"foreground": "#000", "background": "#fff", "standard": "AA"},
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == "pass"
    assert payload["ratio"] == 21


def test_dispatcher_rejects_non_object_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("[]"))

    assert tools.main(["ralph-result"]) == 1

    assert "tool payload must be a JSON object" in capsys.readouterr().err


def test_upsert_task_creates_and_updates_draft_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    assert invoke_tool(monkeypatch, "upsert-task", task_payload(slug="safe-tool")) == 0
    task_path = tmp_path / ".jri" / "tasks" / "draft" / "safe-tool.md"
    assert task_path.exists()
    assert "created draft task" in capsys.readouterr().out

    assert (
        invoke_tool(
            monkeypatch,
            "upsert-task",
            task_payload(slug="safe-tool", body="Updated behavior lock."),
        )
        == 0
    )
    assert "Updated behavior lock." in task_path.read_text(encoding="utf-8")
    assert "updated draft task" in capsys.readouterr().out


def test_upsert_task_validates_slug_and_acceptance_criteria(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    assert invoke_tool(monkeypatch, "upsert-task", task_payload(slug="../escape")) == 1
    assert "characters not allowed" in capsys.readouterr().err

    assert (
        invoke_tool(
            monkeypatch,
            "upsert-task",
            task_payload(slug="missing-criteria", acceptance_criteria=[]),
        )
        == 1
    )
    assert "acceptance_criteria" in capsys.readouterr().err


def test_task_crud_renames_dependencies_and_deletes_unblocked_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    source = write_draft_task(tmp_path, "source")
    dependent = write_draft_task(tmp_path, "dependent", depends_on=["source"])

    assert (
        invoke_tool(
            monkeypatch,
            "rename-task",
            {"from_slug": "source", "to_slug": "renamed"},
        )
        == 0
    )
    rename_output = capsys.readouterr().out
    assert "renamed draft task" in rename_output
    assert "updated depends_on" in rename_output
    assert not source.exists()
    assert (tmp_path / ".jri" / "tasks" / "draft" / "renamed.md").exists()
    assert "- renamed" in dependent.read_text(encoding="utf-8")

    assert invoke_tool(monkeypatch, "delete-task", {"slug": "dependent"}) == 0
    assert "deleted draft task" in capsys.readouterr().out
    assert not dependent.exists()


def test_delete_task_rejects_promoted_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    promoted_path = tmp_path / ".jri" / "tasks" / "todo" / "already-promoted.md"
    promoted_path.parent.mkdir(parents=True, exist_ok=True)
    promoted_path.write_text("promoted", encoding="utf-8")

    assert invoke_tool(monkeypatch, "delete-task", {"slug": "already-promoted"}) == 1

    assert "refusing to delete promoted task" in capsys.readouterr().err
    assert promoted_path.exists()


def test_delete_task_rejects_draft_with_dependents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    write_draft_task(tmp_path, "base")
    write_draft_task(tmp_path, "blocked", depends_on=["base"])

    assert invoke_tool(monkeypatch, "delete-task", {"slug": "base"}) == 1

    assert "refusing to delete draft task with dependents" in capsys.readouterr().err


def test_edit_draft_task_applies_unique_exact_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    task_path = write_draft_task(tmp_path, "edit-me", body="Alpha block.\n")

    assert (
        invoke_tool(
            monkeypatch,
            "edit-draft-task",
            {
                "slug": "edit-me",
                "edits": [{"oldText": "Alpha block.", "newText": "Beta block."}],
            },
        )
        == 0
    )

    result = json.loads(capsys.readouterr().out)
    assert result["path"] == ".jri/tasks/draft/edit-me.md"
    assert result["replacements"] == 1
    assert "Beta block." in task_path.read_text(encoding="utf-8")


def test_edit_draft_task_rejects_duplicate_old_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    write_draft_task(tmp_path, "duplicates", body="repeat\nrepeat\n")

    assert (
        invoke_tool(
            monkeypatch,
            "edit-draft-task",
            {
                "slug": "duplicates",
                "edits": [{"oldText": "repeat", "newText": "once"}],
            },
        )
        == 1
    )

    assert "matched 2 blocks" in capsys.readouterr().err


def test_edit_draft_task_validates_edit_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    write_draft_task(tmp_path, "invalid-edit")

    assert (
        invoke_tool(
            monkeypatch,
            "edit-draft-task",
            {"slug": "invalid-edit", "edits": [{"oldText": "Original body."}]},
        )
        == 1
    )

    assert "newText" in capsys.readouterr().err


def test_task_operations_reject_symlinked_jri_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    outside = tmp_path / "outside-jri"
    outside.mkdir()
    (tmp_path / ".jri").symlink_to(outside, target_is_directory=True)

    assert invoke_tool(monkeypatch, "upsert-task", task_payload(slug="blocked")) == 1

    assert "refusing to write outside `.jri/tasks/`" in capsys.readouterr().err


def test_readme_exact_edit_happy_path_and_missing_text_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    readme_path = tmp_path / "README.md"
    readme_path.write_text("# Project\n\nOld paragraph.\n", encoding="utf-8")

    assert (
        invoke_tool(
            monkeypatch,
            "edit-readme",
            {"edits": [{"oldText": "Old paragraph.", "newText": "New paragraph."}]},
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["path"] == "README.md"
    assert result["replacements"] == 1
    assert "New paragraph." in readme_path.read_text(encoding="utf-8")

    assert (
        invoke_tool(
            monkeypatch,
            "edit-readme",
            {"edits": [{"oldText": "not present", "newText": "ignored"}]},
        )
        == 1
    )
    assert "was not found" in capsys.readouterr().err


def test_readme_operations_reject_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "outside-readme.md"
    target.write_text("outside", encoding="utf-8")
    (tmp_path / "README.md").symlink_to(target)

    assert invoke_tool(monkeypatch, "read-readme", {}) == 1
    assert "refusing to read symlinked README.md" in capsys.readouterr().err

    assert (
        invoke_tool(
            monkeypatch,
            "edit-readme",
            {"edits": [{"oldText": "outside", "newText": "inside"}]},
        )
        == 1
    )
    assert "refusing to edit symlinked README.md" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "outside"


def test_promote_tasks_maps_check_and_apply_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, list[str]]] = []

    class FakeService:
        def __init__(self, root: Path) -> None:
            assert root == tmp_path

        def check_draft_promotion(self, *, slugs: list[str]) -> list[Task]:
            calls.append(("check", slugs))
            return [make_task("alpha"), make_task("beta")]

        def promote_drafts(self, *, slugs: list[str]) -> list[Task]:
            calls.append(("promote", slugs))
            return [make_task("alpha")]

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tools, "JriService", FakeService)

    assert (
        invoke_tool(
            monkeypatch,
            "promote-tasks",
            {"slugs": ["alpha", "beta"], "check_only": True},
        )
        == 0
    )
    assert "Promotion check passed for 2 draft task(s)." in capsys.readouterr().out

    assert (
        invoke_tool(
            monkeypatch,
            "promote-tasks",
            {"slugs": ["alpha"], "check_only": False},
        )
        == 0
    )
    assert "Promoted 1 draft task(s) to todo." in capsys.readouterr().out
    assert calls == [("check", ["alpha", "beta"]), ("promote", ["alpha"])]


def test_approve_draft_promotion_maps_service_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeService:
        def __init__(self, root: Path) -> None:
            assert root == tmp_path

        def approve_draft_promotion(self, *, slugs: list[str]) -> list[Task]:
            assert slugs == ["alpha"]
            return [make_task("alpha")]

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tools, "JriService", FakeService)

    assert (
        invoke_tool(
            monkeypatch,
            "approve-draft-promotion",
            {"slugs": ["alpha"]},
        )
        == 0
    )

    assert "Approved promotion for 1 draft task(s)." in capsys.readouterr().out


def test_list_and_read_tasks_use_service_payload_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    alpha = make_task("alpha", "todo")
    beta = make_task("beta", "done")

    class FakeService:
        def __init__(self, root: Path) -> None:
            self.paths = SimpleNamespace(tasks_dir=root / ".jri" / "tasks")
            self.git = None

        def status(self) -> dict[str, list[Task]]:
            return {"draft": [], "todo": [alpha], "doing": [], "done": [beta]}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tools, "JriService", FakeService)

    assert invoke_tool(monkeypatch, "list-tasks", {}) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["slug"] for item in listed] == ["alpha", "beta"]

    assert invoke_tool(monkeypatch, "read-tasks", {"slugs": ["beta"]}) == 0
    read = json.loads(capsys.readouterr().out)
    assert read[0]["status"] == "done"
    assert read[0]["slug"] == "beta"

    assert invoke_tool(monkeypatch, "read-tasks", {"slugs": ["missing"]}) == 1
    assert "task not found: missing" in capsys.readouterr().err


def test_ralph_result_without_output_path_reports_no_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("JRI_RESULT_PATH", raising=False)

    assert invoke_tool(monkeypatch, "ralph-result", {"result": "completed"}) == 0

    assert capsys.readouterr().out == "JRI_RESULT_PATH not set"


def test_ralph_result_rejects_incompleted_without_learnings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert invoke_tool(monkeypatch, "ralph-result", {"result": "incompleted"}) == 1

    assert "incompleted requires non-empty learnings" in capsys.readouterr().err


def test_ralph_result_rejects_needs_human_without_required_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        invoke_tool(
            monkeypatch,
            "ralph-result",
            {"result": "needs_human", "blocker": "unclear"},
        )
        == 1
    )

    assert "needs_human requires blocker and human_task" in capsys.readouterr().err


def test_ralph_result_writes_valid_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "result.json"
    monkeypatch.setenv("JRI_RESULT_PATH", str(output_path))

    assert (
        invoke_tool(
            monkeypatch,
            "ralph-result",
            {
                "result": "needs_human",
                "summary": "partially done",
                "blocker": "product decision needed",
                "human_task": {
                    "title": "Choose behavior",
                    "body": "Pick a path.",
                    "acceptance_criteria": ["choice is recorded"],
                },
            },
        )
        == 0
    )

    assert capsys.readouterr().out == "Result recorded: needs_human"
    assert json.loads(output_path.read_text(encoding="utf-8")) == {
        "result": "needs_human",
        "summary": "partially done",
        "blocker": "product decision needed",
        "human_task": {
            "title": "Choose behavior",
            "body": "Pick a path.",
            "acceptance_criteria": ["choice is recorded"],
        },
    }


def test_contrast_check_reports_pass_and_fail() -> None:
    passing = json.loads(
        tools.run_contrast_check(
            {"foreground": "000000", "background": "ffffff", "standard": "AAA"}
        )
    )
    failing = json.loads(
        tools.run_contrast_check(
            {"foreground": "777777", "background": "888888", "standard": "AA"}
        )
    )

    assert passing["result"] == "pass"
    assert passing["threshold"] == 7.0
    assert failing["result"] == "fail"
    assert failing["ratio"] < failing["threshold"]


def test_contrast_check_validates_colors_and_standard(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        invoke_tool(
            monkeypatch,
            "check-contrast",
            {"foreground": "not-hex", "background": "ffffff", "standard": "AA"},
        )
        == 1
    )
    assert "foreground" in capsys.readouterr().err

    assert (
        invoke_tool(
            monkeypatch,
            "check-contrast",
            {"foreground": "000", "background": "fff", "standard": "BAD"},
        )
        == 1
    )
    assert "standard" in capsys.readouterr().err
