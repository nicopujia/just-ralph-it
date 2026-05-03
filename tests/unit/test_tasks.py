import json
import os
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from typing import Literal, cast

import pytest

from jri.core.agents.resources import (
    resource_manifest,
    resource_path,
    resource_relative_path,
)
from jri.core.agents.tools import run_contrast_check, run_upsert_task
from jri.core.git import GitRepo
from jri.core.models import Task, TaskMetadata
from jri.core.tasks import (
    dump_task,
    list_tasks,
    parse_task_file,
    select_next_task,
    validate_draft_promotion,
    validate_state_payload,
    validate_task_metadata,
)
from tests.conftest import run_cli
from tests.helpers import git, write_task


def run_agent_tool(cwd: Path, payload: dict[str, object], tool_name: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "jri.core.agents.tools", tool_name],
        cwd=cwd,
        input=json.dumps(payload),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def read_extension_sources(*names: str) -> str:
    extensions = files("jri.core.agents").joinpath("extensions")
    return "\n".join(extensions.joinpath(name).read_text("utf-8") for name in names)


def test_pi_extension_registers_tools_and_commit_prefix_guard() -> None:
    source = read_extension_sources(
        "jri.ts", "commit-guard.ts", "chat-tools.ts", "ralph-tools.ts"
    )

    assert 'pi.on("tool_call"' in source
    assert 'event.toolName !== "bash"' in source
    assert 'RESERVED_PREFIX = "jri:"' in source
    assert "block: true" in source
    assert 'registerPythonTool(\n    pi,\n    "upsert-task"' in source
    assert 'registerPythonTool(\n    pi,\n    "ralph-result"' in source


def test_pi_extension_does_not_register_validator_approval_tool() -> None:
    source = read_extension_sources("jri.ts", "chat-tools.ts", "ralph-tools.ts")

    assert 'registerPythonTool(\n    pi,\n    "approve-draft-promotion"' not in source


def test_pi_extension_launches_validator_runtime_with_approval_extension() -> None:
    source = read_extension_sources("jri.ts", "validators.ts")

    assert 'name: "interrogator-validator"' in source
    assert 'resourcePath("extensions.validator", packageRoot)' in source
    assert "approve-draft-promotion" in source
    assert 'process.env.JRI_CHAT_RUNTIME === "1"' in source
    assert "SLUG_RE.test(slug)" in source
    assert "delete childEnv.JRI_CHAT_RUNTIME" in source
    assert "env: childEnv" in source


def test_pi_extension_launches_ralph_validator_runtime() -> None:
    source = read_extension_sources("jri.ts", "validators.ts", "ralph-tools.ts")

    assert 'name: "ralph-validator"' in source
    assert 'resourcePath("prompts.ralphValidator", packageRoot)' in source
    assert 'configuredModel(packageRoot, "ralph-validator")' in source
    assert '"read,bash,grep,find,ls,list-tasks,read-tasks,check-contrast"' in source
    assert "CHILD_PI_MAX_BUFFER" in source
    assert 'process.env.JRI_CHAT_RUNTIME === "1"' in source
    assert "registerRalphValidator(pi)" in source


def test_pi_extension_splits_chat_and_ralph_tool_registration() -> None:
    source = read_extension_sources("jri.ts", "chat-tools.ts", "ralph-tools.ts")

    assert "function registerChatTools" in source
    assert "function registerRalphTools" in source
    assert 'process.env.JRI_CHAT_RUNTIME === "1"' in source
    assert 'registerPythonTool(\n    pi,\n    "read-readme"' in source
    assert 'registerPythonTool(\n    pi,\n    "edit-readme"' in source
    assert 'registerPythonTool(\n    pi,\n    "edit-draft-task"' in source
    assert "registerExplorer(pi)" in source


def test_pi_extension_explorer_runs_read_only_child_pi() -> None:
    source = read_extension_sources("jri.ts", "explorer.ts")

    assert 'name: "explore"' in source
    assert 'resourcePath("prompts.explorer", packageRoot)' in source
    assert '"--no-session"' in source
    assert '"--no-extensions"' in source
    assert '"--no-skills"' in source
    assert '"--no-prompt-templates"' in source
    assert '"--no-context-files"' in source
    assert '"read,grep,find,ls,web-search"' in source
    assert "JRI_EXPLORER_RUNTIME" in source
    assert 'name: "web-search"' in source
    assert "https://html.duckduckgo.com/html/" in source
    assert "EXPLORER_MAX_TASKS = 8" in source
    assert "EXPLORER_MAX_CONCURRENCY = 4" in source


def test_validator_extension_registers_approval_tool_only() -> None:
    source = (
        files("jri.core.agents")
        .joinpath("extensions", "jri-validator.ts")
        .read_text("utf-8")
    )

    assert 'registerPythonTool(\n    pi,\n    "approve-draft-promotion"' in source
    assert (
        'runPythonTool("approve-draft-promotion", { slugs: requestedSlugs })' in source
    )
    assert "validator approval is recorded automatically after APPROVED" in source
    assert (
        'event.toolName === "approve-draft-promotion" && !event.isError' not in source
    )
    assert "promote-tasks" not in source
    assert "upsert-task" not in source


def inspect_python_tool_spawn_env(
    tmp_path: Path,
    *,
    env: dict[str, str],
) -> dict[str, object]:
    node = shutil.which("node")
    assert node is not None, "node is required to inspect Python tool env"

    harness = tmp_path / "python_tool_env_harness"
    harness.mkdir(parents=True, exist_ok=True)
    capture_path = harness / "capture.json"
    source = (
        files("jri.core.agents")
        .joinpath("tools", "_run-python-tool.mjs")
        .read_text(encoding="utf-8")
        .replace(
            'import { spawnSync } from "child_process";',
            'import { spawnSync } from "./child_process.mjs";',
            1,
        )
    )
    (harness / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    (harness / "_run-python-tool.mjs").write_text(source, encoding="utf-8")
    (harness / "child_process.mjs").write_text(
        "import { writeFileSync } from 'node:fs';\n"
        "export function spawnSync(command, args, options) {\n"
        "  writeFileSync(\n"
        "    process.env.JRI_CAPTURE_PATH,\n"
        "    JSON.stringify({ command, args, env: options.env }),\n"
        "    'utf-8',\n"
        "  );\n"
        "  return { status: 0, stdout: 'ok\\n', stderr: '' };\n"
        "}\n",
        encoding="utf-8",
    )
    script = (
        "import { runPythonTool } from './_run-python-tool.mjs';\n"
        "runPythonTool('ralph-result', { result: 'completed' });\n"
    )
    subprocess.run(
        [node, "--input-type=module", "--eval", script],
        cwd=harness,
        check=True,
        capture_output=True,
        text=True,
        env={**env, "JRI_CAPTURE_PATH": str(capture_path)},
    )
    return json.loads(capture_path.read_text(encoding="utf-8"))


def make_task(
    slug: str,
    *,
    priority: int = 1,
    assignee: Literal["Ralph", "Human"] = "Ralph",
    depends_on: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
) -> Task:
    return Task(
        path=Path(f"/tmp/{slug}.md"),
        slug=slug,
        metadata=TaskMetadata(
            title=slug.replace("-", " ").title(),
            priority=priority,
            assignee=assignee,
            depends_on=depends_on or [],
            acceptance_criteria=acceptance_criteria or [],
        ),
        body="body",
    )


def make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "JRI Tests")
    git(repo, "config", "user.email", "jri-tests@example.com")
    (repo / "README.md").write_text("# temp repo\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    return repo


def test_select_next_task_uses_dependencies_priority_and_slug_order() -> None:
    tasks = [
        make_task("blocked", priority=0, depends_on=["missing"]),
        make_task("human-task", priority=0, assignee="Human"),
        make_task("beta", priority=1),
        make_task("alpha", priority=1),
    ]

    selected = select_next_task(tasks, done_slugs={"setup"}, doing_tasks=[])

    assert selected is not None
    assert selected.slug == "alpha"


def test_select_next_task_rejects_existing_doing_tasks() -> None:
    with pytest.raises(ValueError, match="already in progress"):
        select_next_task(
            [make_task("alpha")], done_slugs=set(), doing_tasks=[make_task("doing")]
        )


def test_validate_task_metadata_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="assignee"):
        validate_task_metadata({"title": "Bad task", "priority": 1})

    with pytest.raises(ValueError, match="assignee"):
        validate_task_metadata(
            {
                "title": "Bad task",
                "priority": 1,
                "assignee": "Robot",
                "depends_on": [],
                "acceptance_criteria": [],
            }
        )

    with pytest.raises(ValueError, match="priority"):
        validate_task_metadata(
            {
                "title": "Bad task",
                "priority": 9,
                "assignee": "Ralph",
                "depends_on": [],
                "acceptance_criteria": [],
            }
        )

    with pytest.raises(ValueError, match="depends_on"):
        validate_task_metadata(
            {
                "title": "Bad task",
                "priority": 1,
                "assignee": "Ralph",
                "depends_on": ["a", "a"],
                "acceptance_criteria": [],
            }
        )


def test_validate_state_payload_accepts_empty() -> None:
    validate_state_payload({})


def test_validate_state_payload_allows_runtime_process_metadata() -> None:
    validate_state_payload(
        {
            "started_at": 1234567890,
            "process": {
                "loop_pid": 123,
                "child_pid": None,
                "log_path": ".jri/logs/ralph/task.log",
                "detached": True,
            },
        }
    )


def test_validate_state_payload_allows_promotion_record() -> None:
    validate_state_payload(
        {
            "started_at": 1234567890,
            "promotion": {
                "confirmed_at": 1,
                "task_slugs": ["clarify-scope"],
                "content_digests": {
                    "clarify-scope": "a" * 64,
                },
                "target_status": "todo",
            },
        }
    )


def test_validate_state_payload_allows_attempt_result_payload() -> None:
    validate_state_payload(
        {
            "attempts": [
                {
                    "number": 1,
                    "task_slug": "retry-task",
                    "branch": "ralph/main",
                    "started_at": 1,
                    "result": "incompleted",
                    "result_payload": {
                        "result": "incompleted",
                        "summary": "Partial progress.",
                        "learnings": ["Use the existing helper."],
                    },
                }
            ]
        }
    )


def test_packaged_schemas_are_available() -> None:
    assert files("jri.core.schemas").joinpath("task-metadata.json").is_file()
    assert files("jri.core.schemas").joinpath("state.json").is_file()
    scaffold = files("jri.core.template")
    assert scaffold.joinpath("learnings.md").is_file()
    builtins = files("jri.core.agents")
    assert builtins.joinpath("prompts", "interrogator.md").is_file()
    assert builtins.joinpath("prompts", "interrogator-validator.md").is_file()
    assert builtins.joinpath("prompts", "explorer.md").is_file()
    assert builtins.joinpath("prompts", "ralph.md").is_file()
    assert builtins.joinpath("prompts", "ralph-validator.md").is_file()
    assert builtins.joinpath("extensions", "jri.ts").is_file()
    assert builtins.joinpath("extensions", "chat-tools.ts").is_file()
    assert builtins.joinpath("extensions", "common.ts").is_file()
    assert builtins.joinpath("extensions", "commit-guard.ts").is_file()
    assert builtins.joinpath("extensions", "explorer.ts").is_file()
    assert builtins.joinpath("extensions", "jri-validator.ts").is_file()
    assert builtins.joinpath("extensions", "python-bridge.ts").is_file()
    assert builtins.joinpath("extensions", "ralph-tools.ts").is_file()
    assert builtins.joinpath("extensions", "validators.ts").is_file()
    assert builtins.joinpath("tools", "__init__.py").is_file()
    assert builtins.joinpath("tools", "__main__.py").is_file()
    assert builtins.joinpath("tools", "_run-python-tool.mjs").is_file()


def test_agent_resource_manifest_resolves_current_package_resources() -> None:
    expected = {
        "extensions.default": "extensions/jri.ts",
        "extensions.validator": "extensions/jri-validator.ts",
        "prompts.ralph": "prompts/ralph.md",
        "prompts.interrogator": "prompts/interrogator.md",
        "prompts.ralphValidator": "prompts/ralph-validator.md",
        "prompts.interrogatorValidator": "prompts/interrogator-validator.md",
        "prompts.explorer": "prompts/explorer.md",
        "tools.pythonRunner": "tools/_run-python-tool.mjs",
        "themes.modernYellow": "themes/modern-yellow.json",
        "skills.hostedProjects": "skills/hosted-projects/SKILL.md",
        "skills.reverseRalph": "skills/reverse-ralph/SKILL.md",
    }

    assert resource_manifest() == expected
    for resource_id, relative_path in expected.items():
        assert resource_relative_path(resource_id) == relative_path
        resolved = resource_path(resource_id)
        assert str(resolved).endswith(relative_path)
        if resource_id != "themes.modernYellow":
            assert resolved.is_file()


def test_agent_resource_manifest_rejects_invalid_ids() -> None:
    with pytest.raises(ValueError, match="unknown agent resource ID: missing.resource"):
        resource_relative_path("missing.resource")


def test_agent_resource_manifest_rejects_unsafe_paths() -> None:
    from jri.core.agents.resources import _validate_manifest_path

    with pytest.raises(ValueError, match="relative"):
        _validate_manifest_path("bad.absolute", "/etc/passwd")
    with pytest.raises(ValueError, match="traverse"):
        _validate_manifest_path("bad.parent", "../outside")
    with pytest.raises(ValueError, match="POSIX"):
        _validate_manifest_path("bad.separator", "extensions\\jri.ts")


def test_typescript_agent_resource_manifest_agrees_with_python() -> None:
    bun = shutil.which("bun")
    assert bun is not None, "bun is required to check the TypeScript resolver"

    script = """
import {
  resourceManifest,
  resourcePath,
  resourceRelativePath,
} from './src/jri/core/agents/resources.ts';

let invalidIdMessage = '';
try {
  resourceRelativePath('missing.resource');
} catch (error) {
  invalidIdMessage = error instanceof Error ? error.message : String(error);
}

console.log(JSON.stringify({
  manifest: resourceManifest(),
  extensionRelative: resourceRelativePath('extensions.default'),
  extensionPath: resourcePath('extensions.default'),
  invalidIdMessage,
}));
"""

    result = subprocess.run(
        [bun, "--eval", script],
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert payload["manifest"] == resource_manifest()
    assert payload["extensionRelative"] == "extensions/jri.ts"
    assert payload["extensionPath"].endswith("extensions/jri.ts")
    assert payload["invalidIdMessage"] == "unknown agent resource ID: missing.resource"


def test_typescript_agent_resource_manifest_rejects_unsafe_paths(
    tmp_path: Path,
) -> None:
    bun = shutil.which("bun")
    assert bun is not None, "bun is required to check the TypeScript resolver"

    source_dir = Path(__file__).resolve().parents[2] / "src" / "jri" / "core" / "agents"
    harness = tmp_path / "agents"
    harness.mkdir()
    shutil.copyfile(source_dir / "resources.ts", harness / "resources.ts")
    (harness / "resource-manifest.json").write_text(
        json.dumps({"bad.parent": "../outside"}) + "\n",
        encoding="utf-8",
    )

    script = """
import { resourceManifest } from './resources.ts';

try {
  resourceManifest();
} catch (error) {
  console.log(error instanceof Error ? error.message : String(error));
}
"""

    result = subprocess.run(
        [bun, "--eval", script],
        cwd=harness,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == (
        "agent resource 'bad.parent' path must not traverse parents"
    )


def test_run_python_tool_uses_forwarded_pythonpath(tmp_path: Path) -> None:
    captured = inspect_python_tool_spawn_env(
        tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "JRI_PYTHON": sys.executable,
            "JRI_PYTHONPATH": "/tmp/jri-src",
            "PYTHONPATH": "/tmp/existing-path",
        },
    )

    assert captured["command"] == sys.executable
    assert captured["args"] == ["-m", "jri.core.agents.tools", "ralph-result"]
    spawned_env = captured["env"]
    assert isinstance(spawned_env, dict)
    assert cast(dict[str, str], spawned_env)["PYTHONPATH"] == (
        "/tmp/jri-src:/tmp/existing-path"
    )


def test_upsert_task_tool_writes_parseable_draft_and_overwrites(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".jri" / "tasks").mkdir(parents=True)

    created = run_agent_tool(
        repo,
        {
            "title": "Clarify scope",
            "body": "Draft the scope.\n",
            "assignee": "Ralph",
            "priority": 1,
            "depends_on": ["setup"],
            "acceptance_criteria": ["Scope is approved"],
        },
        "upsert-task",
    )

    assert created == "created draft task: .jri/tasks/draft/clarify-scope.md"
    task_path = repo / ".jri" / "tasks" / "draft" / "clarify-scope.md"
    written = task_path.read_text(encoding="utf-8")
    assert written.startswith("---\ntitle: Clarify scope\n")
    assert "{\n" not in written

    created_task = parse_task_file(task_path)
    assert created_task.slug == "clarify-scope"
    assert created_task.metadata.title == "Clarify scope"
    assert created_task.metadata.depends_on == ["setup"]
    assert created_task.metadata.acceptance_criteria == ["Scope is approved"]
    assert created_task.body == "Draft the scope.\n"

    updated = run_agent_tool(
        repo,
        {
            "title": "Clarify scope",
            "slug": "clarify-scope",
            "body": "Refined draft.\n",
            "assignee": "Human",
            "priority": 0,
            "depends_on": [],
            "acceptance_criteria": ["Scope is approved"],
        },
        "upsert-task",
    )

    assert updated == "updated draft task: .jri/tasks/draft/clarify-scope.md"
    updated_task = parse_task_file(task_path)
    assert updated_task.metadata.assignee == "Human"
    assert updated_task.metadata.priority == 0
    assert updated_task.metadata.acceptance_criteria == ["Scope is approved"]
    assert updated_task.body == "Refined draft.\n"


def test_edit_draft_task_tool_applies_exact_replacement(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".jri" / "tasks").mkdir(parents=True)
    run_agent_tool(
        repo,
        {
            "title": "Clarify scope",
            "body": "Draft the scope.\n",
            "assignee": "Ralph",
            "priority": 1,
            "acceptance_criteria": ["Scope is approved"],
        },
        "upsert-task",
    )

    output = run_agent_tool(
        repo,
        {
            "slug": "clarify-scope",
            "edits": [{"oldText": "Draft the scope.", "newText": "Refine the scope."}],
        },
        "edit-draft-task",
    )

    result = json.loads(output)
    assert result["path"] == ".jri/tasks/draft/clarify-scope.md"
    assert result["replacements"] == 1
    assert "-Draft the scope." in result["diff"]
    task = parse_task_file(repo / ".jri" / "tasks" / "draft" / "clarify-scope.md")
    assert task.body == "Refine the scope.\n"


def test_edit_draft_task_tool_rejects_invalid_result(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".jri" / "tasks").mkdir(parents=True)
    run_agent_tool(
        repo,
        {
            "title": "Clarify scope",
            "body": "Draft the scope.\n",
            "assignee": "Ralph",
            "priority": 1,
            "acceptance_criteria": ["Scope is approved"],
        },
        "upsert-task",
    )

    with pytest.raises(subprocess.CalledProcessError):
        run_agent_tool(
            repo,
            {
                "slug": "clarify-scope",
                "edits": [{"oldText": "assignee: Ralph", "newText": "assignee: Bot"}],
            },
            "edit-draft-task",
        )


def test_edit_draft_task_tool_rejects_promoted_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".jri" / "tasks" / "todo").mkdir(parents=True)
    write_task(
        repo,
        status="todo",
        slug="clarify-scope",
        title="Clarify scope",
        priority=1,
        assignee="Ralph",
        body="body",
    )

    with pytest.raises(subprocess.CalledProcessError):
        run_agent_tool(
            repo,
            {
                "slug": "clarify-scope",
                "edits": [{"oldText": "body", "newText": "updated"}],
            },
            "edit-draft-task",
        )


def test_readme_tools_read_and_apply_exact_replacement(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Project\n\nOld summary.\n", encoding="utf-8")

    assert run_agent_tool(repo, {}, "read-readme") == "# Project\n\nOld summary.\n"
    output = run_agent_tool(
        repo,
        {"edits": [{"oldText": "Old summary.", "newText": "New summary."}]},
        "edit-readme",
    )

    result = json.loads(output)
    assert result["path"] == "README.md"
    assert result["replacements"] == 1
    assert "+New summary." in result["diff"]
    assert (repo / "README.md").read_text(encoding="utf-8") == (
        "# Project\n\nNew summary.\n"
    )


def test_edit_readme_tool_rejects_missing_old_text(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Project\n", encoding="utf-8")

    with pytest.raises(subprocess.CalledProcessError):
        run_agent_tool(
            repo,
            {"edits": [{"oldText": "Missing", "newText": "Replacement"}]},
            "edit-readme",
        )


def test_readme_tools_reject_symlink(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "README.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (repo / "README.md").symlink_to(outside)

    with pytest.raises(subprocess.CalledProcessError):
        run_agent_tool(repo, {}, "read-readme")
    with pytest.raises(subprocess.CalledProcessError):
        run_agent_tool(
            repo,
            {"edits": [{"oldText": "Outside", "newText": "Inside"}]},
            "edit-readme",
        )


def test_upsert_task_tool_rejects_invalid_slug(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(
        subprocess.CalledProcessError,
        match="returned non-zero exit status",
    ):
        run_agent_tool(
            repo,
            {
                "title": "Clarify scope",
                "slug": "../escape",
                "body": "Draft the scope.\n",
                "assignee": "Ralph",
                "priority": 1,
                "acceptance_criteria": ["Scope is approved"],
            },
            "upsert-task",
        )


def test_run_upsert_task_accepts_75_char_titles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / ".jri" / "tasks").mkdir(parents=True)
    monkeypatch.chdir(repo)

    title = "a" * 75
    result = run_upsert_task(
        {
            "title": title,
            "body": "Draft the scope.\n",
            "assignee": "Ralph",
            "priority": 1,
            "acceptance_criteria": ["Scope is approved"],
        }
    )

    assert result == f"created draft task: .jri/tasks/draft/{title}.md"
    task = parse_task_file(repo / ".jri" / "tasks" / "draft" / f"{title}.md")
    assert task.metadata.title == title


def test_run_upsert_task_rejects_titles_over_75_chars() -> None:
    with pytest.raises(ValueError, match="75 characters or fewer"):
        run_upsert_task(
            {
                "title": "a" * 76,
                "body": "Draft the scope.\n",
                "assignee": "Ralph",
                "priority": 1,
                "acceptance_criteria": ["Scope is approved"],
            }
        )


def test_promote_task_tool_rejects_non_slug_entries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(
        subprocess.CalledProcessError,
        match="returned non-zero exit status",
    ):
        run_agent_tool(
            repo,
            {"slugs": ["title: Build README\npriority: 1"], "check_only": True},
            "promote-tasks",
        )


def test_approve_draft_promotion_tool_records_approval(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    assert run_cli(["init"], cwd=repo) == 0
    write_task(
        repo,
        status="draft",
        slug="clarify-scope",
        title="Clarify scope",
        priority=1,
        assignee="Ralph",
        body="Clarify the scope.\n",
        acceptance_criteria=["Scope is approved"],
    )

    output = run_agent_tool(
        repo,
        {"slugs": ["clarify-scope"]},
        "approve-draft-promotion",
    )

    assert output == "Approved promotion for 1 draft task(s).\n  - clarify-scope"


def test_contrast_check_matches_webaim_thresholds() -> None:
    result = json.loads(
        run_contrast_check(
            {"foreground": "777777", "background": "FFFFFF", "standard": "AA"}
        )
    )

    assert result == {
        "standard": "AA",
        "ratio": 4.48,
        "threshold": 4.5,
        "result": "fail",
    }


def test_contrast_check_supports_foreground_alpha() -> None:
    result = json.loads(
        run_contrast_check(
            {
                "foreground": "0000FF80",
                "background": "FFFFFF",
                "standard": "GraphicsAA",
            }
        )
    )

    assert result == {
        "standard": "GraphicsAA",
        "ratio": 3.29,
        "threshold": 3.0,
        "result": "pass",
    }


def test_contrast_check_tool_executes_via_agent_tool_module(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    result = json.loads(
        run_agent_tool(
            repo,
            {"foreground": "000000", "background": "FFFFFF", "standard": "AAA"},
            "check-contrast",
        )
    )

    assert result["ratio"] == 21.0
    assert result["result"] == "pass"


def test_contrast_check_rejects_invalid_hex() -> None:
    with pytest.raises(ValueError, match="`foreground` must be a valid"):
        run_contrast_check(
            {"foreground": "blue", "background": "FFFFFF", "standard": "AA"}
        )


def test_contrast_check_rejects_invalid_standard() -> None:
    with pytest.raises(ValueError, match="`standard` must be one of"):
        run_contrast_check(
            {
                "foreground": "000000",
                "background": "FFFFFF",
                "standard": "normal-text",
            }
        )


def test_read_tasks_tool_reads_requested_slugs(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    assert run_cli(["init"], cwd=repo) == 0
    write_task(
        repo,
        status="draft",
        slug="clarify-scope",
        title="Clarify scope",
        priority=1,
        assignee="Ralph",
        body="Clarify the scope.\n",
        acceptance_criteria=["Scope is approved"],
    )
    write_task(
        repo,
        status="todo",
        slug="ship-ui",
        title="Ship UI",
        priority=2,
        assignee="Human",
        body="Ship the UI.\n",
        acceptance_criteria=["UI is shipped"],
    )

    single = json.loads(
        run_agent_tool(
            repo,
            {"slugs": ["clarify-scope"]},
            "read-tasks",
        )
    )
    multiple = json.loads(
        run_agent_tool(
            repo,
            {"slugs": ["ship-ui", "clarify-scope"]},
            "read-tasks",
        )
    )

    assert [task["slug"] for task in single] == ["clarify-scope"]
    assert single[0]["status"] == "draft"
    assert single[0]["acceptance_criteria"] == ["Scope is approved"]
    assert [task["slug"] for task in multiple] == ["ship-ui", "clarify-scope"]
    assert [task["status"] for task in multiple] == ["todo", "draft"]


def test_read_tasks_tool_rejects_missing_or_invalid_inputs(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    assert run_cli(["init"], cwd=repo) == 0

    with pytest.raises(
        subprocess.CalledProcessError,
        match="returned non-zero exit status",
    ):
        run_agent_tool(repo, {}, "read-tasks")

    with pytest.raises(
        subprocess.CalledProcessError,
        match="returned non-zero exit status",
    ):
        run_agent_tool(
            repo,
            {"slugs": []},
            "read-tasks",
        )


def test_list_tasks_tool_lists_and_filters_by_status(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    assert run_cli(["init"], cwd=repo) == 0
    write_task(
        repo,
        status="draft",
        slug="clarify-scope",
        title="Clarify scope",
        priority=1,
        assignee="Ralph",
        body="Clarify the scope.\n",
        acceptance_criteria=["Scope is approved"],
    )
    write_task(
        repo,
        status="done",
        slug="setup",
        title="Setup",
        priority=0,
        assignee="Ralph",
        body="Setup done.\n",
        acceptance_criteria=["Setup is complete"],
    )

    all_tasks = json.loads(run_agent_tool(repo, {}, "list-tasks"))
    done_tasks = json.loads(
        run_agent_tool(
            repo,
            {"status": "done"},
            "list-tasks",
        )
    )

    assert [task["slug"] for task in all_tasks] == ["clarify-scope", "setup"]
    assert [task["status"] for task in all_tasks] == ["draft", "done"]
    assert done_tasks == [
        {
            "status": "done",
            "slug": "setup",
            "path": str(repo / ".jri" / "tasks" / "done" / "setup.md"),
            "title": "Setup",
            "priority": 0,
            "assignee": "Ralph",
            "depends_on": [],
            "acceptance_criteria": ["Setup is complete"],
            "body": "Setup done.\n",
        }
    ]


def test_list_tasks_tool_rejects_invalid_status(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    assert run_cli(["init"], cwd=repo) == 0

    with pytest.raises(
        subprocess.CalledProcessError,
        match="returned non-zero exit status",
    ):
        run_agent_tool(
            repo,
            {"status": "blocked"},
            "list-tasks",
        )


def test_upsert_task_tool_rejects_symlinked_draft_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".jri" / "tasks").mkdir(parents=True)
    (repo / ".jri" / "tasks" / "draft").symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        subprocess.CalledProcessError,
        match="returned non-zero exit status",
    ):
        run_agent_tool(
            repo,
            {
                "title": "Clarify scope",
                "body": "Draft the scope.\n",
                "assignee": "Ralph",
                "priority": 1,
                "acceptance_criteria": ["Scope is approved"],
            },
            "upsert-task",
        )

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("symlink_path", [".jri", ".jri/tasks"])
def test_upsert_task_tool_rejects_symlinked_parent_dir(
    tmp_path: Path, symlink_path: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = repo / symlink_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        subprocess.CalledProcessError,
        match="returned non-zero exit status",
    ):
        run_agent_tool(
            repo,
            {
                "title": "Clarify scope",
                "body": "Draft the scope.\n",
                "assignee": "Ralph",
                "priority": 1,
                "acceptance_criteria": ["Scope is approved"],
            },
            "upsert-task",
        )

    assert list(outside.iterdir()) == []


def test_upsert_task_tool_rejects_symlinked_draft_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    draft_dir = repo / ".jri" / "tasks" / "draft"
    draft_dir.mkdir(parents=True)
    (draft_dir / "clarify-scope.md").symlink_to(outside)

    with pytest.raises(
        subprocess.CalledProcessError,
        match="returned non-zero exit status",
    ):
        run_agent_tool(
            repo,
            {
                "title": "Clarify scope",
                "slug": "clarify-scope",
                "body": "Draft the scope.\n",
                "assignee": "Ralph",
                "priority": 1,
                "acceptance_criteria": ["Scope is approved"],
            },
            "upsert-task",
        )

    assert outside.read_text(encoding="utf-8") == "outside\n"


@pytest.mark.parametrize("criteria", [None, []])
def test_upsert_task_tool_requires_non_empty_acceptance_criteria(
    tmp_path: Path, criteria: list[str] | None
) -> None:
    repo = tmp_path / "repo"
    (repo / ".jri" / "tasks").mkdir(parents=True)

    with pytest.raises(
        subprocess.CalledProcessError,
        match="returned non-zero exit status",
    ):
        run_agent_tool(
            repo,
            {
                "title": "Clarify scope",
                "body": "Draft the scope.\n",
                "assignee": "Ralph",
                "priority": 1,
                "acceptance_criteria": criteria,
            },
            "upsert-task",
        )


def test_parse_task_file_reads_frontmatter_and_body(tmp_path: Path) -> None:
    task_path = tmp_path / "build-readme.md"
    task_path.write_text(
        "---\n"
        '{"title": "Build README", "priority": 1, '
        '"assignee": "Ralph", "depends_on": ["prep"], '
        '"acceptance_criteria": ["README exists"]}'
        "\n---\n\nWrite the README body.\n",
        encoding="utf-8",
    )

    task = parse_task_file(task_path)

    assert task.slug == "build-readme"
    assert task.metadata.depends_on == ["prep"]
    assert task.body == "Write the README body.\n"


def test_parse_task_file_rejects_missing_frontmatter_start(tmp_path: Path) -> None:
    task_path = tmp_path / "broken-task.md"
    task_path.write_text("title: Broken\n---\n\nBody\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must start with YAML frontmatter"):
        parse_task_file(task_path)


def test_parse_task_file_rejects_non_object_frontmatter(tmp_path: Path) -> None:
    task_path = tmp_path / "broken-task.md"
    task_path.write_text("---\n- not-an-object\n---\n\nBody\n", encoding="utf-8")

    with pytest.raises(ValueError, match="frontmatter must be an object"):
        parse_task_file(task_path)


def test_parse_task_file_allows_fenced_code_in_frontmatter_block_scalars(
    tmp_path: Path,
) -> None:
    task_path = tmp_path / "document-parser.md"
    task_path.write_text(
        "---\n"
        'title: "Document ``parser``"\n'
        "priority: 1\n"
        'assignee: "Ralph"\n'
        "notes: |\n"
        "  ```yaml\n"
        "  ---\n"
        "  example: true\n"
        "  ```\n"
        "---\n\n"
        "Explain the parser fix.\n",
        encoding="utf-8",
    )

    task = parse_task_file(task_path)

    assert task.slug == "document-parser"
    assert task.metadata.title == "Document ``parser``"
    assert task.body == "Explain the parser fix.\n"


def test_parse_task_file_allows_markdown_like_plain_scalars_in_frontmatter(
    tmp_path: Path,
) -> None:
    task_path = tmp_path / "implement-computer-ai.md"
    task_path.write_text(
        "---\n"
        "title: Implement computer opponents\n"
        "priority: 1\n"
        "assignee: Ralph\n"
        "depends_on:\n"
        "  - implement-tripos-engine\n"
        "acceptance_criteria:\n"
        "  - The app exposes two computer difficulties: `Normal` and `Insane`.\n"
        "  - Automated tests cover at least: legal move rejection and "
        "perfect-play choices.\n"
        "---\n\n"
        "Implement the computer players.\n",
        encoding="utf-8",
    )

    task = parse_task_file(task_path)

    assert task.metadata.title == "Implement computer opponents"
    assert task.metadata.depends_on == ["implement-tripos-engine"]
    assert task.metadata.acceptance_criteria == [
        "The app exposes two computer difficulties: `Normal` and `Insane`.",
        "Automated tests cover at least: legal move rejection and "
        "perfect-play choices.",
    ]


def test_parse_task_file_round_trips_dump_task_output(tmp_path: Path) -> None:
    task_path = tmp_path / "round-trip.md"
    task = Task(
        path=task_path,
        slug="round-trip",
        metadata=TaskMetadata(
            title='Quote "and" slash \\ test',
            priority=2,
            assignee="Ralph",
            depends_on=["setup"],
            acceptance_criteria=["It round-trips"],
        ),
        body="First line\n\nSecond line\n",
    )

    task_path.write_text(dump_task(task), encoding="utf-8")

    parsed = parse_task_file(task_path)

    assert parsed == task


def test_list_tasks_allows_in_place_edits_for_draft_tasks(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="draft",
        slug="clarify-scope",
        title="Clarify scope",
        priority=1,
        assignee="Ralph",
        body="Initial draft body.\n",
    )
    git(git_repo, "add", ".jri/tasks/draft/clarify-scope.md")
    git(git_repo, "commit", "-m", "add draft task")

    draft_path = git_repo / ".jri" / "tasks" / "draft" / "clarify-scope.md"
    draft_path.write_text(
        draft_path.read_text(encoding="utf-8") + "\nStill being clarified.\n",
        encoding="utf-8",
    )

    tasks = list_tasks(draft_path.parent, git_repo=GitRepo(git_repo))

    assert [task.slug for task in tasks] == ["clarify-scope"]
    assert tasks[0].body.endswith("Still being clarified.\n")


def test_validate_draft_promotion_rejects_missing_acceptance_criteria() -> None:
    with pytest.raises(ValueError, match="acceptance_criteria"):
        validate_draft_promotion(
            [make_task("clarify-scope")],
            all_draft_slugs={"clarify-scope"},
            promoted_slugs=set(),
        )


def test_validate_draft_promotion_rejects_dependency_on_unpromoted_draft() -> None:
    with pytest.raises(ValueError, match="outside the promotion batch"):
        validate_draft_promotion(
            [
                make_task(
                    "build-ui",
                    depends_on=["clarify-scope"],
                    acceptance_criteria=["UI exists"],
                )
            ],
            all_draft_slugs={"clarify-scope", "build-ui"},
            promoted_slugs=set(),
        )


def test_validate_draft_promotion_allows_batch_internal_dependencies() -> None:
    validate_draft_promotion(
        [
            make_task("clarify-scope", acceptance_criteria=["scope is clear"]),
            make_task(
                "build-ui",
                depends_on=["clarify-scope"],
                acceptance_criteria=["UI exists"],
            ),
        ],
        all_draft_slugs={"clarify-scope", "build-ui"},
        promoted_slugs=set(),
    )


def test_validate_draft_promotion_rejects_unknown_dependencies() -> None:
    with pytest.raises(ValueError, match="unknown dependency"):
        validate_draft_promotion(
            [
                make_task(
                    "build-ui",
                    depends_on=["missing-task"],
                    acceptance_criteria=["UI exists"],
                )
            ],
            all_draft_slugs={"build-ui"},
            promoted_slugs=set(),
        )


def test_validate_draft_promotion_allows_acyclic_graph() -> None:
    validate_draft_promotion(
        [
            make_task("setup", acceptance_criteria=["setup done"]),
            make_task(
                "build-ui",
                depends_on=["setup"],
                acceptance_criteria=["UI exists"],
            ),
            make_task(
                "build-api",
                depends_on=["setup"],
                acceptance_criteria=["API exists"],
            ),
            make_task(
                "integrate",
                depends_on=["build-ui", "build-api"],
                acceptance_criteria=["integrated"],
            ),
        ],
        all_draft_slugs={"setup", "build-ui", "build-api", "integrate"},
        promoted_slugs=set(),
    )


def test_validate_draft_promotion_rejects_direct_cycle() -> None:
    with pytest.raises(ValueError, match="cyclic dependency"):
        validate_draft_promotion(
            [
                make_task(
                    "alpha",
                    depends_on=["beta"],
                    acceptance_criteria=["alpha done"],
                ),
                make_task(
                    "beta",
                    depends_on=["alpha"],
                    acceptance_criteria=["beta done"],
                ),
            ],
            all_draft_slugs={"alpha", "beta"},
            promoted_slugs=set(),
        )


def test_validate_draft_promotion_rejects_transitive_cycle() -> None:
    with pytest.raises(ValueError, match="cyclic dependency"):
        validate_draft_promotion(
            [
                make_task(
                    "alpha",
                    depends_on=["gamma"],
                    acceptance_criteria=["alpha done"],
                ),
                make_task(
                    "beta",
                    depends_on=["alpha"],
                    acceptance_criteria=["beta done"],
                ),
                make_task(
                    "gamma",
                    depends_on=["beta"],
                    acceptance_criteria=["gamma done"],
                ),
            ],
            all_draft_slugs={"alpha", "beta", "gamma"},
            promoted_slugs=set(),
        )


def test_validate_draft_promotion_rejects_cycle_spanning_promoted_tasks() -> None:
    with pytest.raises(ValueError, match="cyclic dependency"):
        validate_draft_promotion(
            [
                make_task(
                    "new-task",
                    depends_on=["existing-a"],
                    acceptance_criteria=["new done"],
                ),
            ],
            all_draft_slugs={"new-task"},
            promoted_slugs={"existing-a"},
            promoted_deps={"existing-a": ["new-task"]},
        )


def test_validate_draft_promotion_allows_new_task_depending_on_promoted() -> None:
    validate_draft_promotion(
        [
            make_task(
                "new-task",
                depends_on=["existing-a"],
                acceptance_criteria=["new done"],
            ),
        ],
        all_draft_slugs={"new-task"},
        promoted_slugs={"existing-a"},
        promoted_deps={"existing-a": []},
    )


def test_validate_draft_promotion_rejects_self_dependency() -> None:
    with pytest.raises(ValueError, match="cyclic dependency"):
        validate_draft_promotion(
            [
                make_task(
                    "recursive",
                    depends_on=["recursive"],
                    acceptance_criteria=["done"],
                ),
            ],
            all_draft_slugs={"recursive"},
            promoted_slugs=set(),
        )
