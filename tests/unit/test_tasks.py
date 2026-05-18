import json
import os
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from typing import Literal, cast

import pytest
import yaml

import jri.core.tasks as tasks_module
from jri.core.agents.resources import resource_manifest, resource_path, resource_relative_path
from jri.core.agents.tools import run_contrast_check, run_upsert_task
from jri.core.git import GitRepo
from jri.core.models import CompilerTaskSpec, Task, TaskMetadata
from jri.core.tasks import (
    create_task_batch,
    dump_task,
    list_tasks,
    parse_task_file,
    select_next_task,
    validate_state_payload,
    validate_task_metadata,
)
from tests.conftest import run_cli
from tests.helpers import git, write_task

PI_REQUIRED_THEME_COLOR_TOKENS = (
    "accent",
    "border",
    "borderAccent",
    "borderMuted",
    "success",
    "error",
    "warning",
    "muted",
    "dim",
    "text",
    "thinkingText",
    "selectedBg",
    "userMessageBg",
    "userMessageText",
    "customMessageBg",
    "customMessageText",
    "customMessageLabel",
    "toolPendingBg",
    "toolSuccessBg",
    "toolErrorBg",
    "toolTitle",
    "toolOutput",
    "mdHeading",
    "mdLink",
    "mdLinkUrl",
    "mdCode",
    "mdCodeBlock",
    "mdCodeBlockBorder",
    "mdQuote",
    "mdQuoteBorder",
    "mdHr",
    "mdListBullet",
    "toolDiffAdded",
    "toolDiffRemoved",
    "toolDiffContext",
    "syntaxComment",
    "syntaxKeyword",
    "syntaxFunction",
    "syntaxVariable",
    "syntaxString",
    "syntaxNumber",
    "syntaxType",
    "syntaxOperator",
    "syntaxPunctuation",
    "thinkingOff",
    "thinkingMinimal",
    "thinkingLow",
    "thinkingMedium",
    "thinkingHigh",
    "thinkingXhigh",
    "bashMode",
)


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


def read_agent_sources(*names: str) -> str:
    agents = files("jri.core.agents.bundle")
    return "\n".join(agents.joinpath(name).read_text("utf-8") for name in names)


def test_pi_extension_registers_tools_and_commit_prefix_guard() -> None:
    source = read_agent_sources("extension.ts", "(shared)/commits.ts", "ralph/tools.ts")

    assert 'pi.on("tool_call"' in source
    assert 'event.toolName !== "bash"' in source
    assert 'RESERVED_PREFIX = "jri:"' in source
    assert "block: true" in source
    assert 'registerPythonTool(\n    pi,\n    "ralph-result"' in source


def test_pi_extension_does_not_register_validator_approval_tool() -> None:
    source = read_agent_sources("extension.ts", "interrogator/tools.ts", "ralph/tools.ts")

    removed_tool = "approve" + "-draft" + "-promotion"
    assert f'registerPythonTool(\n    pi,\n    "{removed_tool}"' not in source


def test_pi_extension_does_not_launch_interrogator_validator_runtime() -> None:
    source = read_agent_sources("extension.ts", "interrogator/tools.ts")

    removed_agent = "interrogator" + "-validator"
    removed_tool = "approve" + "-draft" + "-promotion"
    assert f'name: "{removed_agent}"' not in source
    assert 'resourcePath("extensions.validator", packageRoot)' not in source
    assert removed_tool not in source
    assert 'process.env.JRI_CHAT_RUNTIME === "1"' in source
    assert "JRI_INTERROGATOR_VALIDATOR_RUNTIME" not in source


def test_pi_extension_launches_ralph_validator_runtime() -> None:
    source = read_agent_sources("extension.ts", "ralph/validator/tools.ts", "ralph/tools.ts")

    assert 'name: "ralph-validator"' in source
    assert 'resourcePath("prompts.ralphValidator", packageRoot)' in source
    assert 'configuredModel(packageRoot, "ralph-validator")' in source
    assert 'agentSkillPaths(packageRoot, "ralph/validator")' in source
    assert '"read,bash,grep,find,ls,list-tasks,read-tasks,check-contrast"' in source
    assert "CHILD_PI_MAX_BUFFER" in source
    assert "VALIDATOR_TIMEOUT_MS" in source
    assert "runUntilTerminalOutput" in source
    assert 'process.env.JRI_CHAT_RUNTIME === "1"' in source
    assert "registerRalphValidator(pi)" in source


def test_pi_extension_splits_chat_and_ralph_tool_registration() -> None:
    source = read_agent_sources("extension.ts", "interrogator/tools.ts", "ralph/tools.ts")

    assert "function registerChatTools" in source
    assert "function registerRalphTools" in source
    assert 'process.env.JRI_CHAT_RUNTIME === "1"' in source
    assert 'registerPythonTool(\n    pi,\n    "read-readme"' in source
    assert 'registerPythonTool(\n    pi,\n    "edit-readme"' in source
    assert 'registerPythonTool(\n    pi,\n    "edit-draft-task"' not in source
    assert 'registerPythonTool(\n    pi,\n    "upsert-task"' not in read_agent_sources("interrogator/tools.ts")
    assert "registerExplorer(pi)" in source


def test_interrogator_registers_intent_graph_tools() -> None:
    source = read_agent_sources("interrogator/tools.ts")

    for tool_name in (
        "create-node",
        "list-nodes",
        "read-node",
        "search-nodes",
        "apply-graph-patch",
        "update-node-metadata",
        "move-node",
        "compile-graph",
    ):
        assert f'registerPythonTool(\n    pi,\n    "{tool_name}"' in source

    assert "Compile the Intent Graph into validated todo tasks" in source
    assert "semantic Intent Graph path" in source
    assert "NODE.md" not in source


def test_pi_extension_explorer_runs_read_only_child_pi() -> None:
    source = read_agent_sources("extension.ts", "explorer/tools.ts")

    assert 'name: "explore"' in source
    assert 'resourcePath("prompts.explorer", packageRoot)' in source
    assert '"--no-session"' in source
    assert '"--no-extensions"' in source
    assert '"--no-skills"' in source
    assert '"--no-prompt-templates"' in source
    assert '"--no-context-files"' in source
    assert 'agentSkillPaths(packageRoot, "explorer")' in source
    assert '"read,grep,find,ls,fetch-url,web-search"' in source
    assert "JRI_EXPLORER_RUNTIME" in source
    assert 'name: "fetch-url"' in source
    assert "validatePublicHttpUrl" in source
    assert "WEB_FETCH_MAX_REDIRECTS = 5" in source
    assert "WEB_FETCH_MAX_CHARS = 200_000" in source
    assert "private or local hosts are not allowed" in source
    assert 'name: "web-search"' in source
    assert "https://html.duckduckgo.com/html/" in source
    assert "EXPLORER_MAX_TASKS = 8" in source
    assert "EXPLORER_MAX_CONCURRENCY = 4" in source
    assert "EXPLORER_TASK_TIMEOUT_MS" in source
    assert "WEB_SEARCH_TIMEOUT_MS" in source
    assert "AbortController" in source
    assert "process.kill(-child.pid" in source


def test_interrogator_validator_resources_are_not_in_manifest() -> None:
    manifest = resource_manifest()

    assert "extensions.validator" not in manifest
    assert "prompts.interrogatorValidator" not in manifest


def inspect_python_tool_spawn_env(tmp_path: Path, *, env: dict[str, str]) -> dict[str, object]:
    bun = shutil.which("bun")
    assert bun is not None, "bun is required to inspect TypeScript Python tool env"

    harness = tmp_path / "python_tool_env_harness"
    harness.mkdir(parents=True, exist_ok=True)
    capture_path = harness / "capture.json"
    source = (
        files("jri.core.agents.bundle")
        .joinpath("(shared)", "runner.ts")
        .read_text(encoding="utf-8")
        .replace(
            'import { spawnSync } from "node:child_process";', 'import { spawnSync } from "./child_process.ts";', 1
        )
        .replace(
            ('import { PYTHON_TOOL_MAX_BUFFER, PYTHON_TOOL_TIMEOUT_MS } from "./subagents.ts";'),
            "const PYTHON_TOOL_MAX_BUFFER = 4 * 1024 * 1024;\nconst PYTHON_TOOL_TIMEOUT_MS = 30_000;",
            1,
        )
    )
    (harness / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    (harness / "runner.ts").write_text(source, encoding="utf-8")
    (harness / "child_process.ts").write_text(
        "import { writeFileSync } from 'node:fs';\n"
        "type SpawnOptions = {\n"
        "  env: Record<string, string>;\n"
        "  maxBuffer?: number;\n"
        "  timeout?: number;\n"
        "  killSignal?: string;\n"
        "};\n"
        "export function spawnSync(\n"
        "  command: string,\n"
        "  args: string[],\n"
        "  options: SpawnOptions,\n"
        ") {\n"
        "  writeFileSync(\n"
        "    process.env.JRI_CAPTURE_PATH,\n"
        "    JSON.stringify({ command, args, options }),\n"
        "    'utf-8',\n"
        "  );\n"
        "  return { status: 0, stdout: 'ok\\n', stderr: '' };\n"
        "}\n",
        encoding="utf-8",
    )
    script = "import { runPythonTool } from './runner.ts';\nrunPythonTool('ralph-result', { result: 'completed' });\n"
    subprocess.run(
        [bun, "--eval", script],
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


def compiler_spec(
    title: str,
    *,
    priority: int = 1,
    assignee: Literal["Ralph", "Human"] = "Ralph",
    depends_on: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    body: str = "Complete the task.\n",
) -> CompilerTaskSpec:
    return CompilerTaskSpec(
        title=title,
        priority=priority,
        assignee=assignee,
        depends_on=depends_on or [],
        acceptance_criteria=(["The task is complete"] if acceptance_criteria is None else acceptance_criteria),
        body=body,
    )


def make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "nicopujia")
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
        select_next_task([make_task("alpha")], done_slugs=set(), doing_tasks=[make_task("doing")])


def test_validate_task_metadata_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="assignee"):
        validate_task_metadata({"title": "Bad task", "priority": 1})

    with pytest.raises(ValueError, match="assignee"):
        validate_task_metadata({
            "title": "Bad task",
            "priority": 1,
            "assignee": "Robot",
            "depends_on": [],
            "acceptance_criteria": [],
        })

    with pytest.raises(ValueError, match="priority"):
        validate_task_metadata({
            "title": "Bad task",
            "priority": 9,
            "assignee": "Ralph",
            "depends_on": [],
            "acceptance_criteria": [],
        })

    with pytest.raises(ValueError, match="depends_on"):
        validate_task_metadata({
            "title": "Bad task",
            "priority": 1,
            "assignee": "Ralph",
            "depends_on": ["a", "a"],
            "acceptance_criteria": [],
        })

    with pytest.raises(ValueError, match="unexpected key"):
        validate_task_metadata({
            "title": "Bad task",
            "priority": 1,
            "assignee": "Ralph",
            "depends_on": [],
            "acceptance_criteria": [],
            "status": "draft",
        })


def test_validate_task_metadata_defaults_optional_lists() -> None:
    metadata = validate_task_metadata({"title": "Good task", "priority": 0, "assignee": "Human"})

    assert metadata == TaskMetadata(
        title="Good task", priority=0, assignee="Human", depends_on=[], acceptance_criteria=[]
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"title": 12, "priority": 1, "assignee": "Ralph"}, "title"),
        ({"title": "a" * 76, "priority": 1, "assignee": "Ralph"}, "75"),
        ({"title": "Task", "priority": True, "assignee": "Ralph"}, "priority"),
        ({"title": "Task", "priority": 1, "assignee": "Ralph", "depends_on": "setup"}, "depends_on"),
        ({"title": "Task", "priority": 1, "assignee": "Ralph", "depends_on": [1]}, r"depends_on\[0\]"),
        ({"title": "Task", "priority": 1, "assignee": "Ralph", "acceptance_criteria": "done"}, "acceptance_criteria"),
        (
            {"title": "Task", "priority": 1, "assignee": "Ralph", "acceptance_criteria": [False]},
            r"acceptance_criteria\[0\]",
        ),
    ],
)
def test_validate_task_metadata_reports_field_contract_errors(payload: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_task_metadata(payload)


def test_validate_state_payload_accepts_empty() -> None:
    validate_state_payload({})


def test_validate_state_payload_allows_runtime_process_metadata() -> None:
    validate_state_payload({
        "started_at": 1234567890,
        "process": {"loop_pid": 123, "child_pid": None, "log_path": ".jri/logs/ralph/task.log", "detached": True},
    })


def test_validate_state_payload_allows_metrics() -> None:
    validate_state_payload({"metrics": [{"task": "task-a", "ts": "2026-04-05T14:30:00Z", "result": "pass"}]})


def test_validate_state_payload_rejects_promotion_record() -> None:
    with pytest.raises(ValueError, match="promotion"):
        validate_state_payload({
            "started_at": 1234567890,
            "promotion": {
                "confirmed_at": 1,
                "task_slugs": ["clarify-scope"],
                "content_digests": {"clarify-scope": "a" * 64},
                "target_status": "todo",
            },
        })


def test_validate_state_payload_allows_attempt_result_payload() -> None:
    validate_state_payload({
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
    })


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"attempts": "one"}, "attempts"),
        ({"started_at": True}, "started_at"),
        ({"session": 1}, "session"),
        ({"process": "running"}, "process"),
        ({"process": {"pid": 1}}, "process.pid"),
        ({"process": {"loop_pid": False}}, "process.loop_pid"),
        ({"process": {"child_pid": "123"}}, "process.child_pid"),
        ({"process": {"log_path": 3}}, "process.log_path"),
        ({"process": {"detached": "yes"}}, "process.detached"),
        ({"metrics": {}}, "metrics"),
        ({"metrics": [1]}, r"metrics\[0\]"),
        ({"metrics": [{"task": "a", "ts": "t", "result": "pass", "extra": True}]}, r"metrics\[0\].extra"),
        ({"metrics": [{"ts": "t", "result": "pass"}]}, r"metrics\[0\].task"),
        ({"metrics": [{"task": 1, "ts": "t", "result": "pass"}]}, r"metrics\[0\].task"),
        ({"metrics": [{"task": "a", "ts": 1, "result": "pass"}]}, r"metrics\[0\].ts"),
        ({"metrics": [{"task": "a", "ts": "t", "result": "ok"}]}, r"metrics\[0\].result"),
        ({"active_attempt": "task"}, "active_attempt"),
        ({"active_attempt": {"number": 1, "task_slug": "task", "branch": "main"}}, "active_attempt.started_at"),
        (
            {"active_attempt": {"number": "1", "task_slug": "task", "branch": "main", "started_at": 1}},
            "active_attempt.number",
        ),
        (
            {"active_attempt": {"number": 1, "task_slug": 3, "branch": "main", "started_at": 1}},
            "active_attempt.task_slug",
        ),
        (
            {"active_attempt": {"number": 1, "task_slug": "task", "branch": "main", "started_at": 1, "result": "ok"}},
            "known attempt result",
        ),
        (
            {"active_attempt": {"number": 1, "task_slug": "task", "branch": "main", "started_at": 1, "extra": True}},
            "active_attempt.extra",
        ),
        (
            {
                "active_attempt": {
                    "number": 1,
                    "task_slug": "task",
                    "branch": "main",
                    "started_at": 1,
                    "result_payload": "done",
                }
            },
            "result_payload",
        ),
        (
            {
                "active_attempt": {
                    "number": 1,
                    "task_slug": "task",
                    "branch": "main",
                    "started_at": 1,
                    "result_payload": {"result": "failed"},
                }
            },
            "completed, incompleted, needs_human",
        ),
        (
            {
                "active_attempt": {
                    "number": 1,
                    "task_slug": "task",
                    "branch": "main",
                    "started_at": 1,
                    "result_payload": {"result": "completed", "summary": 1},
                }
            },
            "summary",
        ),
        (
            {
                "active_attempt": {
                    "number": 1,
                    "task_slug": "task",
                    "branch": "main",
                    "started_at": 1,
                    "result_payload": {"result": "completed", "learnings": "none"},
                }
            },
            "learnings",
        ),
        (
            {
                "active_attempt": {
                    "number": 1,
                    "task_slug": "task",
                    "branch": "main",
                    "started_at": 1,
                    "result_payload": {"result": "completed", "learnings": [2]},
                }
            },
            r"learnings\[0\]",
        ),
        (
            {
                "active_attempt": {
                    "number": 1,
                    "task_slug": "task",
                    "branch": "main",
                    "started_at": 1,
                    "result_payload": {"result": "completed", "human_task": "ask"},
                }
            },
            "human_task",
        ),
        (
            {
                "active_attempt": {
                    "number": 1,
                    "task_slug": "task",
                    "branch": "main",
                    "started_at": 1,
                    "result_payload": {"result": "completed", "human_task": {"title": 1, "body": "Ask", "extra": True}},
                }
            },
            "human_task.extra",
        ),
    ],
)
def test_validate_state_payload_reports_nested_contract_errors(payload: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_state_payload(payload)


def test_validate_state_payload_reports_extra_result_payload_and_human_priority() -> None:
    with pytest.raises(ValueError, match="result_payload.extra"):
        validate_state_payload({
            "active_attempt": {
                "number": 1,
                "task_slug": "task",
                "branch": "main",
                "started_at": 1,
                "result_payload": {"result": "completed", "extra": True},
            }
        })

    with pytest.raises(ValueError, match="human_task.priority"):
        validate_state_payload({
            "active_attempt": {
                "number": 1,
                "task_slug": "task",
                "branch": "main",
                "started_at": 1,
                "result_payload": {
                    "result": "needs_human",
                    "human_task": {"title": "Ask human", "body": "Please decide.", "priority": True},
                },
            }
        })


def test_non_schema_package_resources_are_available() -> None:
    scaffold = files("jri.core.template")
    assert scaffold.joinpath("learnings.md").is_file()
    assert scaffold.joinpath("graph", ".gitkeep").is_file()
    builtins = files("jri.core.agents.bundle")
    assert builtins.joinpath("interrogator", "prompt.md").is_file()
    assert builtins.joinpath("compiler", "prompt.md").is_file()
    assert builtins.joinpath("explorer", "prompt.md").is_file()
    assert builtins.joinpath("ralph", "prompt.md").is_file()
    assert builtins.joinpath("ralph", "validator", "prompt.md").is_file()
    assert builtins.joinpath("ralph", "skills", "project-setup", "SKILL.md").is_file()
    assert builtins.joinpath("extension.ts").is_file()
    assert builtins.joinpath("interrogator", "tools.ts").is_file()
    assert builtins.joinpath("(shared)", "registry.ts").is_file()
    assert builtins.joinpath("(shared)", "commits.ts").is_file()
    assert builtins.joinpath("(shared)", "assets.ts").is_file()
    assert builtins.joinpath("manifest.json").is_file()
    assert builtins.joinpath("theme.json").is_file()
    assert builtins.joinpath("explorer", "tools.ts").is_file()
    assert builtins.joinpath("(shared)", "subagents.ts").is_file()
    assert builtins.joinpath("ralph", "tools.ts").is_file()
    assert builtins.joinpath("ralph", "validator", "tools.ts").is_file()
    assert builtins.joinpath("(shared)", "runner.ts").is_file()


def test_agent_resource_manifest_resolves_current_package_resources() -> None:
    expected = {
        "extensions.default": "extension.ts",
        "prompts.ralph": "ralph/prompt.md",
        "prompts.interrogator": "interrogator/prompt.md",
        "prompts.compiler": "compiler/prompt.md",
        "prompts.ralphValidator": "ralph/validator/prompt.md",
        "prompts.explorer": "explorer/prompt.md",
        "tools.pythonRunner": "(shared)/runner.ts",
        "themes.modernYellow": "theme.json",
    }

    assert resource_manifest() == expected
    for resource_id, relative_path in expected.items():
        assert resource_relative_path(resource_id) == relative_path
        resolved = resource_path(resource_id)
        assert str(resolved).endswith(relative_path)
        assert resolved.is_file()


def test_modern_yellow_theme_matches_pi_schema_tokens() -> None:
    theme = json.loads(resource_path("themes.modernYellow").read_text(encoding="utf-8"))

    assert theme["$schema"] == (
        "https://raw.githubusercontent.com/badlogic/pi-mono/main/packages/"
        "coding-agent/src/modes/interactive/theme/theme-schema.json"
    )
    assert theme["name"] == "modern-yellow"
    assert theme["vars"]["primary"].lower() == "#f6c944"
    assert set(theme["colors"]) == set(PI_REQUIRED_THEME_COLOR_TOKENS)
    assert list(theme["colors"]) == list(PI_REQUIRED_THEME_COLOR_TOKENS)


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
        _validate_manifest_path("bad.separator", "extensions\\bad.ts")


def test_typescript_agent_resource_manifest_agrees_with_python() -> None:
    bun = shutil.which("bun")
    assert bun is not None, "bun is required to check the TypeScript resolver"

    script = """
import {
  resourceManifest,
  resourcePath,
  resourceRelativePath,
} from './src/jri/core/agents/bundle/(shared)/assets.ts';

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
        [bun, "--eval", script], cwd=Path(__file__).resolve().parents[2], check=True, capture_output=True, text=True
    )
    payload = json.loads(result.stdout)

    assert payload["manifest"] == resource_manifest()
    assert payload["extensionRelative"] == "extension.ts"
    assert payload["extensionPath"].endswith("extension.ts")
    assert payload["invalidIdMessage"] == "unknown agent resource ID: missing.resource"


def test_typescript_agent_resource_manifest_rejects_unsafe_paths(tmp_path: Path) -> None:
    bun = shutil.which("bun")
    assert bun is not None, "bun is required to check the TypeScript resolver"

    source_dir = Path(__file__).resolve().parents[2] / "src" / "jri" / "core" / "agents" / "bundle" / "(shared)"
    harness = tmp_path / "agents"
    shared = harness / "(shared)"
    shared.mkdir(parents=True)
    shutil.copyfile(source_dir / "assets.ts", shared / "assets.ts")
    (harness / "manifest.json").write_text(json.dumps({"bad.parent": "../outside"}) + "\n", encoding="utf-8")

    script = """
import { resourceManifest } from './(shared)/assets.ts';

try {
  resourceManifest();
} catch (error) {
  console.log(error instanceof Error ? error.message : String(error));
}
"""

    result = subprocess.run([bun, "--eval", script], cwd=harness, check=True, capture_output=True, text=True)

    assert result.stdout.strip() == ("agent resource 'bad.parent' path must not traverse parents")


def test_typescript_agent_skill_paths_lists_only_skill_directories(tmp_path: Path) -> None:
    bun = shutil.which("bun")
    assert bun is not None, "bun is required to check the TypeScript resolver"

    source_dir = Path(__file__).resolve().parents[2] / "src" / "jri" / "core" / "agents" / "bundle" / "(shared)"
    harness = tmp_path / "agents"
    shared = harness / "(shared)"
    shared.mkdir(parents=True)
    shutil.copyfile(source_dir / "assets.ts", shared / "assets.ts")
    (harness / "manifest.json").write_text(json.dumps(resource_manifest()) + "\n", encoding="utf-8")
    (harness / "explorer" / "skills" / "beta").mkdir(parents=True)
    (harness / "explorer" / "skills" / "alpha").mkdir(parents=True)
    (harness / "explorer" / "skills" / "README.md").write_text("ignored\n", encoding="utf-8")

    script = """
import { agentSkillPaths } from './(shared)/assets.ts';

console.log(JSON.stringify({
  explorer: agentSkillPaths(process.cwd(), 'explorer').map((path) => path.split('/').pop()),
  missing: agentSkillPaths(process.cwd(), 'ralph/validator'),
}));
"""

    result = subprocess.run([bun, "--eval", script], cwd=harness, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)

    assert payload == {"explorer": ["alpha", "beta"], "missing": []}


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
    options_obj = captured["options"]
    assert isinstance(options_obj, dict)
    options = cast(dict[str, object], options_obj)
    assert options["timeout"] == 30_000
    assert options["maxBuffer"] == 4 * 1024 * 1024
    assert options["killSignal"] == "SIGTERM"
    spawned_env = options["env"]
    assert isinstance(spawned_env, dict)
    assert cast(dict[str, str], spawned_env)["PYTHONPATH"] == ("/tmp/jri-src:/tmp/existing-path")


def test_upsert_task_tool_writes_parseable_todo_and_refuses_overwrite(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".jri" / "tasks").mkdir(parents=True)

    created = run_agent_tool(
        repo,
        {
            "title": "Clarify scope",
            "body": "Write the scope.\n",
            "assignee": "Ralph",
            "priority": 1,
            "depends_on": ["setup"],
            "acceptance_criteria": ["Scope is approved"],
        },
        "upsert-task",
    )

    assert created == "created todo task: .jri/tasks/todo/clarify-scope.md"
    task_path = repo / ".jri" / "tasks" / "todo" / "clarify-scope.md"
    written = task_path.read_text(encoding="utf-8")
    assert written.startswith("---\ntitle: Clarify scope\n")
    assert "{\n" not in written

    created_task = parse_task_file(task_path)
    assert created_task.slug == "clarify-scope"
    assert created_task.metadata.title == "Clarify scope"
    assert created_task.metadata.depends_on == ["setup"]
    assert created_task.metadata.acceptance_criteria == ["Scope is approved"]
    assert created_task.body == "Write the scope.\n"

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        run_agent_tool(
            repo,
            {
                "title": "Clarify scope",
                "slug": "clarify-scope",
                "body": "Refined scope.\n",
                "assignee": "Human",
                "priority": 0,
                "acceptance_criteria": ["Scope is approved"],
            },
            "upsert-task",
        )
    assert "refusing to overwrite symlinked todo task" not in exc_info.value.stderr
    assert "refusing to overwrite existing todo task" in exc_info.value.stderr


def test_readme_tools_read_and_apply_exact_replacement(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Project\n\nOld summary.\n", encoding="utf-8")

    assert run_agent_tool(repo, {}, "read-readme") == "# Project\n\nOld summary.\n"
    output = run_agent_tool(repo, {"edits": [{"oldText": "Old summary.", "newText": "New summary."}]}, "edit-readme")

    result = json.loads(output)
    assert result["path"] == "README.md"
    assert result["replacements"] == 1
    assert "+New summary." in result["diff"]
    assert (repo / "README.md").read_text(encoding="utf-8") == ("# Project\n\nNew summary.\n")


def test_edit_readme_tool_rejects_missing_old_text(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Project\n", encoding="utf-8")

    with pytest.raises(subprocess.CalledProcessError):
        run_agent_tool(repo, {"edits": [{"oldText": "Missing", "newText": "Replacement"}]}, "edit-readme")


def test_readme_tools_reject_symlink(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "README.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (repo / "README.md").symlink_to(outside)

    with pytest.raises(subprocess.CalledProcessError):
        run_agent_tool(repo, {}, "read-readme")
    with pytest.raises(subprocess.CalledProcessError):
        run_agent_tool(repo, {"edits": [{"oldText": "Outside", "newText": "Inside"}]}, "edit-readme")


def test_upsert_task_tool_rejects_invalid_slug(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(subprocess.CalledProcessError, match="returned non-zero exit status"):
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


def test_run_upsert_task_accepts_75_char_titles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    (repo / ".jri" / "tasks").mkdir(parents=True)
    monkeypatch.chdir(repo)

    title = "a" * 75
    result = run_upsert_task({
        "title": title,
        "body": "Draft the scope.\n",
        "assignee": "Ralph",
        "priority": 1,
        "acceptance_criteria": ["Scope is approved"],
    })

    assert result == f"created todo task: .jri/tasks/todo/{title}.md"
    task = parse_task_file(repo / ".jri" / "tasks" / "todo" / f"{title}.md")
    assert task.metadata.title == title


def test_run_upsert_task_rejects_titles_over_75_chars() -> None:
    with pytest.raises(ValueError, match="75 characters or fewer"):
        run_upsert_task({
            "title": "a" * 76,
            "body": "Draft the scope.\n",
            "assignee": "Ralph",
            "priority": 1,
            "acceptance_criteria": ["Scope is approved"],
        })


def test_promote_task_tool_rejects_non_slug_entries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(subprocess.CalledProcessError, match="returned non-zero exit status"):
        run_agent_tool(repo, {"slugs": ["title: Build README\npriority: 1"], "check_only": True}, "promote-tasks")


def test_removed_draft_task_tools_are_not_registered(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    for tool_name in ("edit-draft-task", "rename-task", "delete-task"):
        with pytest.raises(subprocess.CalledProcessError, match="returned non-zero exit status"):
            run_agent_tool(repo, {"slug": "clarify-scope"}, tool_name)


def test_removed_validator_approval_tool_is_not_available(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    assert run_cli(["init"], cwd=repo) == 0

    with pytest.raises(subprocess.CalledProcessError, match="returned non-zero exit status"):
        run_agent_tool(repo, {"slugs": ["clarify-scope"]}, "approve" + "-draft" + "-promotion")


def test_contrast_check_matches_webaim_thresholds() -> None:
    result = json.loads(run_contrast_check({"foreground": "777777", "background": "FFFFFF", "standard": "AA"}))

    assert result == {"standard": "AA", "ratio": 4.48, "threshold": 4.5, "result": "fail"}


def test_contrast_check_supports_foreground_alpha() -> None:
    result = json.loads(
        run_contrast_check({"foreground": "0000FF80", "background": "FFFFFF", "standard": "GraphicsAA"})
    )

    assert result == {"standard": "GraphicsAA", "ratio": 3.29, "threshold": 3.0, "result": "pass"}


def test_contrast_check_tool_executes_via_agent_tool_module(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    result = json.loads(
        run_agent_tool(repo, {"foreground": "000000", "background": "FFFFFF", "standard": "AAA"}, "check-contrast")
    )

    assert result["ratio"] == 21.0
    assert result["result"] == "pass"


def test_contrast_check_rejects_invalid_hex() -> None:
    with pytest.raises(ValueError, match="`foreground` must be a valid"):
        run_contrast_check({"foreground": "blue", "background": "FFFFFF", "standard": "AA"})


def test_contrast_check_rejects_invalid_standard() -> None:
    with pytest.raises(ValueError, match="`standard` must be one of"):
        run_contrast_check({"foreground": "000000", "background": "FFFFFF", "standard": "normal-text"})


def test_read_tasks_tool_reads_requested_slugs(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    assert run_cli(["init"], cwd=repo) == 0
    write_task(
        repo,
        status="todo",
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

    single = json.loads(run_agent_tool(repo, {"slugs": ["clarify-scope"]}, "read-tasks"))
    multiple = json.loads(run_agent_tool(repo, {"slugs": ["ship-ui", "clarify-scope"]}, "read-tasks"))

    assert [task["slug"] for task in single] == ["clarify-scope"]
    assert single[0]["status"] == "todo"
    assert single[0]["acceptance_criteria"] == ["Scope is approved"]
    assert [task["slug"] for task in multiple] == ["ship-ui", "clarify-scope"]
    assert [task["status"] for task in multiple] == ["todo", "todo"]


def test_read_tasks_tool_rejects_missing_or_invalid_inputs(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    assert run_cli(["init"], cwd=repo) == 0

    with pytest.raises(subprocess.CalledProcessError, match="returned non-zero exit status"):
        run_agent_tool(repo, {}, "read-tasks")

    with pytest.raises(subprocess.CalledProcessError, match="returned non-zero exit status"):
        run_agent_tool(repo, {"slugs": []}, "read-tasks")


def test_list_tasks_tool_lists_and_filters_by_status(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    assert run_cli(["init"], cwd=repo) == 0
    write_task(
        repo,
        status="todo",
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
    done_tasks = json.loads(run_agent_tool(repo, {"status": "done"}, "list-tasks"))

    assert [task["slug"] for task in all_tasks] == ["clarify-scope", "setup"]
    assert [task["status"] for task in all_tasks] == ["todo", "done"]
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

    with pytest.raises(subprocess.CalledProcessError, match="returned non-zero exit status"):
        run_agent_tool(repo, {"status": "blocked"}, "list-tasks")


def test_upsert_task_tool_rejects_symlinked_todo_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".jri" / "tasks").mkdir(parents=True)
    (repo / ".jri" / "tasks" / "todo").symlink_to(outside, target_is_directory=True)

    with pytest.raises(subprocess.CalledProcessError, match="returned non-zero exit status"):
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
def test_upsert_task_tool_rejects_symlinked_parent_dir(tmp_path: Path, symlink_path: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = repo / symlink_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside, target_is_directory=True)

    with pytest.raises(subprocess.CalledProcessError, match="returned non-zero exit status"):
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


def test_upsert_task_tool_rejects_symlinked_todo_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    todo_dir = repo / ".jri" / "tasks" / "todo"
    todo_dir.mkdir(parents=True)
    (todo_dir / "clarify-scope.md").symlink_to(outside)

    with pytest.raises(subprocess.CalledProcessError, match="returned non-zero exit status"):
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
def test_upsert_task_tool_requires_non_empty_acceptance_criteria(tmp_path: Path, criteria: list[str] | None) -> None:
    repo = tmp_path / "repo"
    (repo / ".jri" / "tasks").mkdir(parents=True)

    with pytest.raises(subprocess.CalledProcessError, match="returned non-zero exit status"):
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


def test_parse_task_file_reads_body_without_blank_separator(tmp_path: Path) -> None:
    task_path = tmp_path / "compact-task.md"
    task_path.write_text(
        "---\n"
        "title: Compact task\n"
        "priority: 1\n"
        "assignee: Ralph\n"
        "acceptance_criteria:\n"
        "  - It parses\n"
        "---\n"
        "Body starts immediately.\n",
        encoding="utf-8",
    )

    task = parse_task_file(task_path)

    assert task.body == "Body starts immediately.\n"


def test_parse_task_file_rejects_invalid_filename_slug(tmp_path: Path) -> None:
    task_path = tmp_path / "bad slug.md"
    task_path.write_text("---\ntitle: Bad slug\npriority: 1\nassignee: Ralph\n---\n\nBody\n", encoding="utf-8")

    with pytest.raises(ValueError, match="task filename `bad slug.md`"):
        parse_task_file(task_path)


def test_parse_task_file_rejects_missing_frontmatter_start(tmp_path: Path) -> None:
    task_path = tmp_path / "broken-task.md"
    task_path.write_text("title: Broken\n---\n\nBody\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must start with YAML frontmatter"):
        parse_task_file(task_path)


def test_parse_task_file_rejects_missing_frontmatter_end(tmp_path: Path) -> None:
    task_path = tmp_path / "broken-task.md"
    task_path.write_text("---\ntitle: Broken\npriority: 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must end frontmatter"):
        parse_task_file(task_path)


def test_parse_task_file_rejects_non_object_frontmatter(tmp_path: Path) -> None:
    task_path = tmp_path / "broken-task.md"
    task_path.write_text("---\n- not-an-object\n---\n\nBody\n", encoding="utf-8")

    with pytest.raises(ValueError, match="frontmatter must be an object"):
        parse_task_file(task_path)


def test_parse_task_file_allows_fenced_code_in_frontmatter_block_scalars(tmp_path: Path) -> None:
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


def test_parse_task_file_allows_markdown_like_plain_scalars_in_frontmatter(tmp_path: Path) -> None:
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
        "Automated tests cover at least: legal move rejection and perfect-play choices.",
    ]


def test_parse_task_file_preserves_block_scalar_boundaries(tmp_path: Path) -> None:
    task_path = tmp_path / "block-scalar-task.md"
    task_path.write_text(
        "---\n"
        "title: Block scalar task\n"
        "priority: 1\n"
        "assignee: Ralph\n"
        "acceptance_criteria:\n"
        "  - It parses\n"
        "notes: >\n"
        "  This line is inside the scalar.\n"
        "\n"
        "  So is this line.\n"
        "---\n\n"
        "Body after a folded scalar.\n",
        encoding="utf-8",
    )

    task = parse_task_file(task_path)

    assert task.slug == "block-scalar-task"
    assert task.body == "Body after a folded scalar.\n"


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


def test_list_tasks_rejects_in_place_edits_for_todo_tasks(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="clarify-scope",
        title="Clarify scope",
        priority=1,
        assignee="Ralph",
        body="Initial draft body.\n",
        acceptance_criteria=["Scope is clear"],
    )
    git(git_repo, "add", ".jri/tasks/todo/clarify-scope.md")
    git(git_repo, "commit", "-m", "add todo task")

    todo_path = git_repo / ".jri" / "tasks" / "todo" / "clarify-scope.md"
    todo_path.write_text(todo_path.read_text(encoding="utf-8") + "\nMutated in place.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="modified in place"):
        list_tasks(todo_path.parent, git_repo=GitRepo(git_repo))


def test_list_tasks_returns_empty_for_missing_directory(tmp_path: Path) -> None:
    assert list_tasks(tmp_path / "missing") == []


def test_list_tasks_sorts_by_slug_and_wraps_malformed_files(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "zeta.md").write_text(
        dump_task(make_task("zeta", acceptance_criteria=["Zeta done"])), encoding="utf-8"
    )
    (tasks_dir / "alpha.md").write_text(
        dump_task(make_task("alpha", acceptance_criteria=["Alpha done"])), encoding="utf-8"
    )

    assert [task.slug for task in list_tasks(tasks_dir)] == ["alpha", "zeta"]

    (tasks_dir / "broken.md").write_text("not frontmatter\n", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed task file `broken.md`"):
        list_tasks(tasks_dir)


def test_lifecycle_task_files_require_acceptance_criteria(tmp_path: Path) -> None:
    todo_dir = tmp_path / ".jri" / "tasks" / "todo"
    todo_dir.mkdir(parents=True)
    task_path = todo_dir / "missing-criteria.md"
    task_path.write_text("---\ntitle: Missing criteria\npriority: 1\nassignee: Ralph\n---\n\nBody\n", encoding="utf-8")

    with pytest.raises(ValueError, match="non-empty acceptance_criteria"):
        parse_task_file(task_path)


def test_create_task_batch_accepts_existing_done_dependency_and_normalizes_slug(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".jri" / "tasks" / "done").mkdir(parents=True)
    write_task(
        repo,
        status="done",
        slug="setup_done",
        title="Setup done",
        priority=1,
        assignee="Ralph",
        body="Existing setup.\n",
        acceptance_criteria=["Setup exists"],
    )

    tasks = create_task_batch(
        repo,
        [
            compiler_spec(
                "  Ship API v2!!!  ",
                depends_on=["setup_done"],
                acceptance_criteria=["API task exists"],
                body="Ship it.\n",
            )
        ],
    )

    assert [task.slug for task in tasks] == ["ship-api-v2"]
    created = parse_task_file(repo / ".jri" / "tasks" / "todo" / "ship-api-v2.md")
    assert created.metadata.depends_on == ["setup_done"]
    assert created.body == "Ship it.\n"


@pytest.mark.parametrize(
    ("specs", "message"),
    [
        ([compiler_spec("Body", body="   ")], "body"),
        ([compiler_spec("   ")], "title"),
        ([compiler_spec("Unknown dep", depends_on=["missing"])], "unknown dependency"),
        ([compiler_spec("Empty dep", depends_on=[" "])], "non-empty string"),
        ([compiler_spec("Bad dep", depends_on=["../escape"])], "not allowed"),
        ([compiler_spec("No criteria", acceptance_criteria=[])], "acceptance_criteria"),
    ],
)
def test_create_task_batch_rejects_invalid_compiler_specs_without_writes(
    tmp_path: Path, specs: list[CompilerTaskSpec], message: str
) -> None:
    repo = tmp_path / "repo"

    with pytest.raises(ValueError, match=message):
        create_task_batch(repo, specs)

    assert not (repo / ".jri" / "tasks" / "todo").exists()


def test_create_task_batch_rejects_symlinked_todo_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".jri" / "tasks").mkdir(parents=True)
    (repo / ".jri" / "tasks" / "todo").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="outside `.jri/tasks/todo`"):
        create_task_batch(repo, [compiler_spec("Safe write")])

    assert list(outside.iterdir()) == []


def test_parse_task_file_rejects_draft_status_frontmatter(tmp_path: Path) -> None:
    task_path = tmp_path / "draft-task.md"
    task_path.write_text(
        "---\n"
        "title: Clarify scope\n"
        "priority: 1\n"
        "assignee: Ralph\n"
        "status: draft\n"
        "acceptance_criteria:\n"
        "  - Scope is clear\n"
        "---\n\n"
        "Clarify the scope.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unexpected key.*status"):
        parse_task_file(task_path)


def test_create_task_batch_ignores_cleanup_failure_after_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    original_write_text = Path.write_text
    original_unlink = Path.unlink

    def write_text_with_second_task_failure(self: Path, data: str, encoding: str | None = None) -> int:
        if self.name == "second-task.md":
            raise OSError("disk full")
        return original_write_text(self, data, encoding=encoding)

    def unlink_with_cleanup_failure(self: Path, *, missing_ok: bool = False) -> None:
        if self.name == "first-task.md":
            raise OSError("cleanup failed")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "write_text", write_text_with_second_task_failure)
    monkeypatch.setattr(Path, "unlink", unlink_with_cleanup_failure)

    with pytest.raises(OSError, match="disk full"):
        create_task_batch(repo, [compiler_spec("First task"), compiler_spec("Second task")])

    first_task = repo / ".jri" / "tasks" / "todo" / "first-task.md"
    assert parse_task_file(first_task).slug == "first-task"


def test_create_task_batch_rejects_racy_existing_todo_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    todo_dir = repo / ".jri" / "tasks" / "todo"
    todo_dir.mkdir(parents=True)
    (todo_dir / "safe-write.md").write_text("existing\n", encoding="utf-8")

    def fake_existing_lifecycle_task_slugs(root: Path) -> set[str]:
        del root
        return set()

    monkeypatch.setattr(tasks_module, "_existing_lifecycle_task_slugs", fake_existing_lifecycle_task_slugs)

    with pytest.raises(ValueError, match="refusing to overwrite existing task"):
        create_task_batch(repo, [compiler_spec("Safe write")])


def test_create_task_batch_rejects_racy_symlink_escape_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    todo_dir = repo / ".jri" / "tasks" / "todo"
    todo_dir.mkdir(parents=True)
    (todo_dir / "safe-write.md").symlink_to(outside)

    def fake_existing_lifecycle_task_slugs(root: Path) -> set[str]:
        del root
        return set()

    monkeypatch.setattr(tasks_module, "_existing_lifecycle_task_slugs", fake_existing_lifecycle_task_slugs)

    with pytest.raises(ValueError, match="outside `.jri/tasks/todo`"):
        create_task_batch(repo, [compiler_spec("Safe write")])

    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_parse_task_file_uses_scanner_when_yaml_parser_finds_no_document_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_path = tmp_path / "scanner-fallback.md"
    task_path.write_text(
        "---\n"
        "title: Scanner fallback\n"
        "priority: 1\n"
        "assignee: Ralph\n"
        "acceptance_criteria:\n"
        "  - It parses\n"
        "---\n\n"
        "Body\n",
        encoding="utf-8",
    )

    def fake_parse_empty(text: str) -> list[object]:
        del text
        return []

    monkeypatch.setattr(tasks_module.yaml, "parse", fake_parse_empty)

    task = parse_task_file(task_path)

    assert task.metadata.title == "Scanner fallback"
    assert task.body == "Body\n"


def test_parse_task_file_rejects_frontmatter_when_yaml_mark_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_path = tmp_path / "missing-mark.md"
    task_path.write_text(
        "---\ntitle: Missing mark\npriority: 1\nassignee: Ralph\nacceptance_criteria:\n  - It parses\n---\n",
        encoding="utf-8",
    )

    def fake_parse_missing_mark(text: str) -> list[object]:
        del text
        return [tasks_module.DocumentEndEvent(None, None, explicit=False)]

    monkeypatch.setattr(tasks_module.yaml, "parse", fake_parse_missing_mark)

    with pytest.raises(ValueError, match="must end frontmatter"):
        parse_task_file(task_path)


def test_parse_task_file_retries_original_frontmatter_when_normalized_yaml_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_path = tmp_path / "retry-normalized.md"
    task_path.write_text(
        "---\n"
        "title: Retry normalized # parser\n"
        "priority: 1\n"
        "assignee: Ralph\n"
        "acceptance_criteria:\n"
        "  - It parses\n"
        "---\n\n"
        "Body\n",
        encoding="utf-8",
    )
    real_safe_load = yaml.safe_load
    calls = 0

    def flaky_safe_load(value: str) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise yaml.YAMLError("normalized parse failed")
        return real_safe_load(value)

    monkeypatch.setattr(tasks_module.yaml, "safe_load", flaky_safe_load)

    task = parse_task_file(task_path)

    assert calls == 2
    assert task.metadata.title == "Retry normalized"


def test_parse_task_file_scanner_ignores_separators_inside_block_scalars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_path = tmp_path / "scanner-block-scalar.md"
    task_path.write_text(
        "---\n"
        "title: Scanner block scalar\n"
        "priority: 1\n"
        "assignee: Ralph\n"
        "acceptance_criteria:\n"
        "  - It parses\n"
        "notes: |\n"
        "  ---\n"
        "  still frontmatter content\n"
        "---\n\n"
        "Body after scalar\n",
        encoding="utf-8",
    )

    def broken_parse(_text: str) -> object:
        raise yaml.YAMLError("force scanner")

    monkeypatch.setattr(tasks_module.yaml, "parse", broken_parse)

    task = parse_task_file(task_path)

    assert task.body == "Body after scalar\n"


def test_parse_task_file_rejects_missing_scanned_boundary_without_trailing_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_path = tmp_path / "unterminated-scanner.md"
    task_path.write_text("---\ntitle: Unterminated", encoding="utf-8")

    def broken_parse(_text: str) -> object:
        raise yaml.YAMLError("force scanner")

    monkeypatch.setattr(tasks_module.yaml, "parse", broken_parse)

    with pytest.raises(ValueError, match="must end frontmatter"):
        parse_task_file(task_path)


def test_parse_task_file_normalizes_comments_block_dedents_and_crlf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_path = tmp_path / "normalize-comments.md"
    text = (
        "---\n"
        "title: Commented title # ignored comment\r\n"
        "priority: 1\r\n"
        "assignee: Ralph\r\n"
        "notes: |\r\n"
        "  Keep this: value\r\n"
        "acceptance_criteria:\r\n"
        "  - It parses: with colon # ignored comment\r\n"
        "---\n\n"
        "Body\r\n"
    )

    def read_exact_text(_self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        assert encoding == "utf-8"
        assert errors is None
        return text

    monkeypatch.setattr(Path, "read_text", read_exact_text)

    task = parse_task_file(task_path)

    assert task.metadata.title == "Commented title"
    assert task.metadata.acceptance_criteria == ["It parses: with colon"]


def test_parse_task_file_rejects_unquoted_empty_and_boolean_plain_scalars(tmp_path: Path) -> None:
    empty_item = tmp_path / "empty-item.md"
    empty_item.write_text(
        "---\ntitle: Empty item\npriority: 1\nassignee: Ralph\nacceptance_criteria:\n  - \n---\n", encoding="utf-8"
    )
    boolean_title = tmp_path / "boolean-title.md"
    boolean_title.write_text(
        "---\ntitle: true\npriority: 1\nassignee: Ralph\nacceptance_criteria:\n  - It parses\n---\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match=r"acceptance_criteria\[0\]"):
        parse_task_file(empty_item)
    with pytest.raises(ValueError, match="title"):
        parse_task_file(boolean_title)


def test_parse_task_file_normalizes_plain_scalar_without_line_ending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_path = tmp_path / "no-line-ending.md"
    text = "---\ntitle: No newline\npriority: 1\nassignee: Ralph\nacceptance_criteria:\n  - Needs: quote---\nBody\n"
    task_path.write_text(text, encoding="utf-8")

    class Mark:
        index = len("---\ntitle: No newline\npriority: 1\nassignee: Ralph\nacceptance_criteria:\n  - Needs: quote")

    def fake_parse_mark(text: str) -> list[object]:
        del text
        return [tasks_module.DocumentEndEvent(Mark(), Mark(), explicit=False)]

    monkeypatch.setattr(tasks_module.yaml, "parse", fake_parse_mark)

    task = parse_task_file(task_path)

    assert task.metadata.acceptance_criteria == ["Needs: quote"]
    assert task.body == "Body\n"
