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
        "run_apply_graph_patch",
        "run_contrast_check",
        "run_create_node",
        "run_edit_readme",
        "run_list_tasks",
        "run_move_node",
        "run_ralph_result",
        "run_read_node",
        "run_read_readme",
        "run_read_tasks",
        "run_update_node_metadata",
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


def make_task(slug: str, status: str = "todo") -> Task:
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


def test_upsert_task_creates_todo_task_and_refuses_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    assert invoke_tool(monkeypatch, "upsert-task", task_payload(slug="safe-tool")) == 0
    task_path = tmp_path / ".jri" / "tasks" / "todo" / "safe-tool.md"
    assert task_path.exists()
    assert "created todo task" in capsys.readouterr().out

    assert (
        invoke_tool(
            monkeypatch,
            "upsert-task",
            task_payload(slug="safe-tool", body="Updated behavior lock."),
        )
        == 1
    )
    assert "refusing to overwrite existing todo task" in capsys.readouterr().err
    assert "Updated behavior lock." not in task_path.read_text(encoding="utf-8")


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


def test_removed_task_crud_tools_are_not_registered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    for tool_name in ("rename-task", "delete-task", "edit-draft-task"):
        assert invoke_tool(monkeypatch, tool_name, {"slug": "safe-tool"}) == 2
        assert "expected one tool name" in capsys.readouterr().err


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


def test_graph_tools_create_read_patch_metadata_and_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    assert (
        invoke_tool(
            monkeypatch,
            "create-node",
            {
                "path": "product/auth/login",
                "title": "Login Flow",
                "body": "Initial behavior.\n",
            },
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created == {
        "path": "product/auth/login",
        "auto_created_parents": ["product", "product/auth"],
    }

    assert invoke_tool(monkeypatch, "read-node", {"path": "product", "depth": 2}) == 0
    read = json.loads(capsys.readouterr().out)
    assert read == {
        "path": "product",
        "metadata": {"title": "Product", "state": "active"},
        "body": "",
        "children": [
            {"path": "product/auth", "title": "Auth", "state": "active"},
            {
                "path": "product/auth/login",
                "title": "Login Flow",
                "state": "active",
            },
        ],
    }
    assert "NODE.md" not in json.dumps(read)

    patch = """*** Begin Graph Patch
*** Update Node: product/auth/login
@@
-Initial behavior.
+Initial behavior.
+Second behavior.
*** End Graph Patch
"""
    assert invoke_tool(monkeypatch, "apply-graph-patch", {"patch": patch}) == 0
    patched = json.loads(capsys.readouterr().out)
    assert patched == {
        "changed_nodes": [
            {"path": "product/auth/login", "additions": 2, "deletions": 1}
        ]
    }

    assert (
        invoke_tool(
            monkeypatch,
            "update-node-metadata",
            {
                "path": "product/auth/login",
                "title": "Password Login",
                "state": "archived",
                "archive_reason": "Replaced by passkeys.",
            },
        )
        == 0
    )
    metadata = json.loads(capsys.readouterr().out)
    assert metadata == {
        "path": "product/auth/login",
        "metadata": {
            "title": "Password Login",
            "state": "archived",
            "archive_reason": "Replaced by passkeys.",
        },
    }

    assert (
        invoke_tool(
            monkeypatch,
            "move-node",
            {"source_path": "product/auth", "destination_path": "product/sign-in"},
        )
        == 0
    )
    moved = json.loads(capsys.readouterr().out)
    assert moved == {
        "old_path": "product/auth",
        "new_path": "product/sign-in",
        "moved_subtree_count": 2,
    }


@pytest.mark.parametrize(
    "tool_name,payload",
    [
        ("create-node", {"path": ".jri/graph/auth/NODE.md", "title": "x", "body": ""}),
        ("read-node", {"path": "/absolute"}),
        ("update-node-metadata", {"path": "../escape", "title": "x"}),
        (
            "move-node",
            {"source_path": "product/auth", "destination_path": "product//auth"},
        ),
        (
            "apply-graph-patch",
            {
                "patch": (
                    "*** Begin Graph Patch\n"
                    "*** Update Node: .jri/graph/auth/NODE.md\n"
                    "@@\n"
                    "+new\n"
                    "*** End Graph Patch\n"
                )
            },
        ),
    ],
)
def test_graph_tools_reject_raw_or_malformed_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tool_name: str,
    payload: dict[str, object],
) -> None:
    monkeypatch.chdir(tmp_path)

    assert invoke_tool(monkeypatch, tool_name, payload) == 1

    error = capsys.readouterr().err
    assert "graph path" in error or "NODE.md" in error


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


def test_promote_tasks_tool_is_not_registered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    for tool_name in ("promote" + "-tasks", "approve" + "-draft" + "-promotion"):
        assert invoke_tool(monkeypatch, tool_name, {"slugs": ["alpha"]}) == 2
        assert "expected one tool name" in capsys.readouterr().err


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
            return {"todo": [alpha], "doing": [], "done": [beta]}

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
