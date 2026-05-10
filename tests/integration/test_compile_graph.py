from collections.abc import Callable
from pathlib import Path
from typing import cast

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
    assert git(git_repo, "status", "--short") == "?? .jri/graph/"


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
    assert git(git_repo, "status", "--short") == "?? .jri/graph/"


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
