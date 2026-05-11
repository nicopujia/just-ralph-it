import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from jri.core.errors import JriError
from jri.core.models import AgentRunResult
from jri.core.service import JriService
from tests.conftest import run_cli
from tests.helpers import git


class FakeCompilerRuntime:
    def __init__(self, result: dict[str, object] | None = None) -> None:
        self.model: str | None = None
        self.result: dict[str, object] = (
            result
            if result is not None
            else cast(
                dict[str, object],
                {
                    "tasks": [
                        {
                            "title": "Build checkout flow",
                            "priority": 1,
                            "assignee": "Ralph",
                            "depends_on": [],
                            "acceptance_criteria": ["Checkout can be completed"],
                            "body": (
                                "Implement the checkout flow from the graph intent.\n"
                            ),
                        },
                        {
                            "title": "Verify checkout flow",
                            "priority": 2,
                            "assignee": "Ralph",
                            "depends_on": ["build-checkout-flow"],
                            "acceptance_criteria": [
                                "Checkout verification is documented"
                            ],
                            "body": "Verify the checkout flow end to end.\n",
                        },
                    ]
                },
            )
        )
        self.compile_calls: list[dict[str, object]] = []
        self.ralph_calls = 0

    def list_sessions(self, *, root: Path, limit: int = 20) -> list[dict[str, object]]:
        return []

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: Callable[[int], None] | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        del root, prompt, log_path, result_path, on_start, timeout
        self.ralph_calls += 1
        raise AssertionError("compile_graph must not start Ralph")

    def export_session(self, session_id: str, destination: Path) -> None:
        raise AssertionError("compile_graph should not export sessions")

    def compile_intent_graph(
        self, *, root: Path, context: dict[str, object]
    ) -> dict[str, object]:
        self.compile_calls.append(context)
        return self.result


class FailingCommitService(JriService):
    def _commit_compiled_graph(self, message: str, paths: list[str]) -> bool:
        raise RuntimeError("git commit failed")


class NoopCommitService(JriService):
    def _commit_compiled_graph(self, message: str, paths: list[str]) -> bool:
        del message, paths
        return False


def _init(repo: Path) -> None:
    assert run_cli(["init"], cwd=repo) == 0


def _write_graph_node(
    repo: Path, semantic_path: str, body: str = "Original intent.\n"
) -> Path:
    parts = semantic_path.split("/")
    for index in range(1, len(parts)):
        parent_path = "/".join(parts[:index])
        _write_single_graph_node(repo, parent_path, "")
    return _write_single_graph_node(repo, semantic_path, body)


def _write_single_graph_node(repo: Path, semantic_path: str, body: str) -> Path:
    node_path = repo / ".jri" / "graph" / semantic_path / "NODE.md"
    node_path.parent.mkdir(parents=True, exist_ok=True)
    node_path.write_text(
        "---\n"
        f"title: {semantic_path.rsplit('/', 1)[-1].replace('-', ' ').title()}\n"
        "state: active\n"
        "---\n\n"
        f"{body}",
        encoding="utf-8",
    )
    return node_path


def _task_paths(repo: Path) -> list[Path]:
    return sorted((repo / ".jri" / "tasks" / "todo").glob("*.md"))


def _head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def _head_files(repo: Path) -> set[str]:
    return set(
        git(
            repo, "show", "--name-only", "--format=", "--no-renames", "HEAD"
        ).splitlines()
    )


def test_compile_graph_commits_graph_changes_and_emitted_tasks(git_repo: Path) -> None:
    _init(git_repo)
    runtime = FakeCompilerRuntime()
    node_path = _write_graph_node(git_repo, "product/checkout", "Need checkout.\n")
    before = _head(git_repo)

    result = JriService(git_repo, agent_runtime=runtime).compile_graph()

    assert result["exit_code"] == "success"
    assert result["task_slugs"] == ["build-checkout-flow", "verify-checkout-flow"]
    assert isinstance(result["commit"], str)
    assert result["commit"] != before
    assert git(git_repo, "rev-parse", "HEAD") == result["commit"]
    assert "jri/" not in git(git_repo, "tag", "--list")
    assert runtime.ralph_calls == 0
    assert len(runtime.compile_calls) == 1
    context = runtime.compile_calls[0]
    assert context["changed_paths"] == ["product", "product/checkout"]
    assert "product/checkout" in str(context["graph_nodes"])
    assert "Need checkout." in str(context["graph_nodes"])
    assert node_path.read_text(encoding="utf-8").endswith("Need checkout.\n")
    assert _head_files(git_repo) == {
        ".jri/graph/product/NODE.md",
        ".jri/graph/product/checkout/NODE.md",
        ".jri/tasks/todo/build-checkout-flow.md",
        ".jri/tasks/todo/verify-checkout-flow.md",
    }
    assert git(git_repo, "status", "--short") == ""


def test_compile_graph_ambiguity_failure_writes_no_tasks_or_commit(
    git_repo: Path,
) -> None:
    _init(git_repo)
    _write_graph_node(git_repo, "product/checkout", "Need checkout or invoice?\n")
    before = _head(git_repo)
    runtime = FakeCompilerRuntime(
        {
            "exit_code": "fail",
            "errors": [
                {
                    "location": "product/checkout",
                    "ambiguous_area": "payment destination",
                    "plausible_interpretations": ["checkout", "invoice"],
                    "draft_question": (
                        "Should Ralph build checkout or invoice payment first?"
                    ),
                }
            ],
        }
    )

    result = JriService(git_repo, agent_runtime=runtime).compile_graph()

    assert result == {
        "exit_code": "fail",
        "errors": [
            {
                "location": "product/checkout",
                "ambiguous_area": "payment destination",
                "plausible_interpretations": ["checkout", "invoice"],
                "draft_question": (
                    "Should Ralph build checkout or invoice payment first?"
                ),
            }
        ],
    }
    assert _head(git_repo) == before
    assert _task_paths(git_repo) == []
    assert git(git_repo, "status", "--short") == "?? .jri/graph/product/"


def test_compile_graph_invalid_compiler_output_rolls_back_tasks(git_repo: Path) -> None:
    _init(git_repo)
    _write_graph_node(git_repo, "product/checkout")
    before = _head(git_repo)
    runtime = FakeCompilerRuntime(
        {
            "tasks": [
                {
                    "title": "Build checkout flow",
                    "priority": 1,
                    "assignee": "Ralph",
                    "depends_on": [],
                    "acceptance_criteria": ["Checkout can be completed"],
                    "body": "Implement checkout.\n",
                },
                {
                    "title": "Broken output",
                    "priority": 1,
                    "assignee": "Ralph",
                    "depends_on": ["missing-task"],
                    "acceptance_criteria": ["Broken output is rejected"],
                    "body": "This should not persist.\n",
                },
            ]
        }
    )

    result = JriService(git_repo, agent_runtime=runtime).compile_graph()

    assert result["exit_code"] == "fail"
    assert "unknown dependency `missing-task`" in str(result["errors"])
    assert _head(git_repo) == before
    assert _task_paths(git_repo) == []
    assert git(git_repo, "status", "--short") == "?? .jri/graph/product/"


def test_compile_graph_commit_failure_rolls_back_emitted_tasks(git_repo: Path) -> None:
    _init(git_repo)
    _write_graph_node(git_repo, "product/checkout")
    before = _head(git_repo)
    runtime = FakeCompilerRuntime()

    result = FailingCommitService(git_repo, agent_runtime=runtime).compile_graph()

    assert result["exit_code"] == "fail"
    assert "git commit failed" in str(result["errors"])
    assert _head(git_repo) == before
    assert _task_paths(git_repo) == []
    assert "build-checkout-flow" not in git(git_repo, "status", "--short")
    assert "verify-checkout-flow" not in git(git_repo, "status", "--short")
    assert "?? .jri/graph/" in git(git_repo, "status", "--short")


def test_compile_graph_does_not_start_ralph(git_repo: Path) -> None:
    _init(git_repo)
    _write_graph_node(git_repo, "product/checkout")
    runtime = FakeCompilerRuntime()

    result = JriService(git_repo, agent_runtime=runtime).compile_graph()

    assert result["exit_code"] == "success"
    assert runtime.ralph_calls == 0
    assert git(git_repo, "tag", "--list") == ""


class NonDictCompilerRuntime:
    def __init__(self) -> None:
        self.model: str | None = None
        self.compile_calls: list[dict[str, object]] = []

    def compile_intent_graph(self, *, root: Path, context: dict[str, object]) -> object:
        del root
        self.compile_calls.append(context)
        return ["not", "an", "object"]


class NoCompilerRuntime:
    def __init__(self) -> None:
        self.model: str | None = None


def test_compile_graph_rejects_malformed_graph_before_compiler(git_repo: Path) -> None:
    _init(git_repo)
    (git_repo / ".jri" / "graph" / "product").mkdir(parents=True)
    runtime = FakeCompilerRuntime()
    before = _head(git_repo)

    result = JriService(git_repo, agent_runtime=runtime).compile_graph()

    assert result == {"exit_code": "fail", "errors": ["product: missing NODE.md"]}
    assert runtime.compile_calls == []
    assert _head(git_repo) == before
    assert _task_paths(git_repo) == []


def test_compile_graph_rejects_when_no_graph_paths_changed(git_repo: Path) -> None:
    _init(git_repo)
    runtime = FakeCompilerRuntime()
    before = _head(git_repo)

    result = JriService(git_repo, agent_runtime=runtime).compile_graph()

    assert result == {
        "exit_code": "fail",
        "errors": ["no uncommitted graph changes to compile"],
    }
    assert runtime.compile_calls == []
    assert _head(git_repo) == before
    assert _task_paths(git_repo) == []


def test_compile_graph_reports_unavailable_compiler(git_repo: Path) -> None:
    _init(git_repo)
    _write_graph_node(git_repo, "product/checkout")
    before = _head(git_repo)

    result = JriService(git_repo, agent_runtime=NoCompilerRuntime()).compile_graph()

    assert result == {
        "exit_code": "fail",
        "errors": ["agent runtime does not provide an intent compiler"],
    }
    assert _head(git_repo) == before
    assert _task_paths(git_repo) == []


def test_compile_graph_reports_non_object_compiler_output(git_repo: Path) -> None:
    _init(git_repo)
    _write_graph_node(git_repo, "product/checkout")
    before = _head(git_repo)
    runtime = NonDictCompilerRuntime()

    result = JriService(git_repo, agent_runtime=runtime).compile_graph()

    assert result == {
        "exit_code": "fail",
        "errors": ["compiler output must be an object"],
    }
    assert len(runtime.compile_calls) == 1
    assert _head(git_repo) == before
    assert _task_paths(git_repo) == []


def test_compile_graph_reports_malformed_failure_payload(git_repo: Path) -> None:
    _init(git_repo)
    _write_graph_node(git_repo, "product/checkout")
    before = _head(git_repo)
    runtime = FakeCompilerRuntime({"exit_code": "fail", "errors": []})

    result = JriService(git_repo, agent_runtime=runtime).compile_graph()

    assert result == {
        "exit_code": "fail",
        "errors": ["compiler failure must include non-empty `errors`"],
    }
    assert _head(git_repo) == before
    assert _task_paths(git_repo) == []


def test_compile_graph_reports_malformed_task_payload(git_repo: Path) -> None:
    _init(git_repo)
    _write_graph_node(git_repo, "product/checkout")
    before = _head(git_repo)
    runtime = FakeCompilerRuntime(
        {
            "tasks": [
                {
                    "title": "Build checkout flow",
                    "priority": True,
                    "assignee": "Ralph",
                    "depends_on": [],
                    "acceptance_criteria": ["Checkout can be completed"],
                    "body": "Implement checkout.\n",
                }
            ]
        }
    )

    result = JriService(git_repo, agent_runtime=runtime).compile_graph()

    assert result == {
        "exit_code": "fail",
        "errors": ["task[0] `priority` must be an integer"],
    }
    assert _head(git_repo) == before
    assert _task_paths(git_repo) == []


def test_compile_graph_context_includes_archived_graph_metadata(git_repo: Path) -> None:
    _init(git_repo)
    _write_graph_node(git_repo, "product/checkout")
    archived = git_repo / ".jri" / "graph" / "product" / "checkout" / "NODE.md"
    archived.write_text(
        "---\n"
        "title: Checkout\n"
        "state: archived\n"
        "archive_reason: Replaced by invoices\n"
        "---\n\n"
        "Archived checkout intent.\n",
        encoding="utf-8",
    )
    runtime = FakeCompilerRuntime()

    result = JriService(git_repo, agent_runtime=runtime).compile_graph()

    assert result["exit_code"] == "success"
    context = runtime.compile_calls[0]
    assert context["graph_check"] == {
        "active_count": 1,
        "archived_count": 1,
        "errors": [],
    }
    graph_nodes = cast(list[dict[str, object]], context["graph_nodes"])
    checkout_node = next(
        node for node in graph_nodes if node["path"] == "product/checkout"
    )
    assert checkout_node["metadata"] == {
        "title": "Checkout",
        "state": "archived",
        "archive_reason": "Replaced by invoices",
    }


def test_compile_graph_reports_noop_commit_and_rolls_back_tasks(
    git_repo: Path,
) -> None:
    _init(git_repo)
    _write_graph_node(git_repo, "product/checkout")
    runtime = FakeCompilerRuntime()

    result = NoopCommitService(git_repo, agent_runtime=runtime).compile_graph()

    assert result == {
        "exit_code": "fail",
        "errors": ["no graph or task changes to commit"],
    }
    assert _task_paths(git_repo) == []


def test_compile_graph_reports_graph_status_command_failure(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init(git_repo)
    _write_graph_node(git_repo, "product/checkout")
    service = JriService(git_repo, agent_runtime=FakeCompilerRuntime())

    def fake_run(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[:3] == ("status", "--porcelain", "--"):
            return subprocess.CompletedProcess(args, 1, "", "status exploded\n")
        return service.git.__class__(git_repo).run(*args, **kwargs)

    monkeypatch.setattr(service.git, "run", fake_run)

    with pytest.raises(JriError, match="status exploded"):
        service.compile_graph()


def test_compile_graph_status_parser_expands_directories_and_renames(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init(git_repo)
    _write_graph_node(git_repo, "product")
    _write_graph_node(git_repo, "product/checkout", "Need checkout.\n")
    runtime = FakeCompilerRuntime()
    service = JriService(git_repo, agent_runtime=runtime)
    real_git = service.git.__class__(git_repo)

    def fake_run(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[:3] == ("status", "--porcelain", "--"):
            return subprocess.CompletedProcess(
                args,
                0,
                "\n".join(
                    (
                        "?? .jri/graph",
                        "R  .jri/graph/old/NODE.md -> "
                        ".jri/graph/product/checkout/NODE.md",
                        "?? .jri/graph/product/",
                        "?? .jri/graph/product/notes.txt",
                        "?? README.md",
                        "?? xx",
                    )
                ),
                "",
            )
        return real_git.run(*args, **kwargs)

    monkeypatch.setattr(service.git, "run", fake_run)

    result = service.compile_graph()

    assert result["exit_code"] == "success"
    assert runtime.compile_calls[0]["changed_paths"] == ["product", "product/checkout"]


@pytest.mark.parametrize(
    ("compiler_result", "message"),
    [
        (
            {"exit_code": "fail", "errors": ["bad"]},
            "compiler error[0] must be an object",
        ),
        (
            {
                "exit_code": "fail",
                "errors": [
                    {
                        "ambiguous_area": "area",
                        "plausible_interpretations": ["one"],
                        "draft_question": "Question?",
                    }
                ],
            },
            "compiler error[0] must include `location`",
        ),
        (
            {
                "exit_code": "fail",
                "errors": [
                    {
                        "location": "product/checkout",
                        "plausible_interpretations": ["one"],
                        "draft_question": "Question?",
                    }
                ],
            },
            "compiler error[0] must include `ambiguous_area`",
        ),
        (
            {
                "exit_code": "fail",
                "errors": [
                    {
                        "location": "product/checkout",
                        "ambiguous_area": "area",
                        "plausible_interpretations": [""],
                        "draft_question": "Question?",
                    }
                ],
            },
            "compiler error[0] must include `plausible_interpretations`",
        ),
        (
            {
                "exit_code": "fail",
                "errors": [
                    {
                        "location": "product/checkout",
                        "ambiguous_area": "area",
                        "plausible_interpretations": ["one"],
                    }
                ],
            },
            "compiler error[0] must include `draft_question`",
        ),
        ({"tasks": []}, "compiler output must include non-empty `tasks`"),
        ({"tasks": ["bad"]}, "task[0] must be an object"),
        (
            {
                "tasks": [
                    {
                        "priority": 1,
                        "assignee": "Ralph",
                        "depends_on": [],
                        "acceptance_criteria": ["ok"],
                        "body": "body",
                    }
                ]
            },
            "task[0] `title` must be a string",
        ),
        (
            {
                "tasks": [
                    {
                        "title": "Task",
                        "priority": 1,
                        "assignee": "Ralph",
                        "depends_on": [1],
                        "acceptance_criteria": ["ok"],
                        "body": "body",
                    }
                ]
            },
            "task[0] `depends_on` must be a string array",
        ),
    ],
)
def test_compile_graph_reports_malformed_compiler_branches(
    git_repo: Path,
    compiler_result: dict[str, object],
    message: str,
) -> None:
    _init(git_repo)
    _write_graph_node(git_repo, "product/checkout")

    result = JriService(
        git_repo,
        agent_runtime=FakeCompilerRuntime(compiler_result),
    ).compile_graph()

    assert result == {"exit_code": "fail", "errors": [message]}
