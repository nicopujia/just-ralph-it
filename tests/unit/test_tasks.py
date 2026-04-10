import json
import os
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from typing import Literal

import pytest

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


def run_upsert_task_tool(cwd: Path, tmp_path: Path, payload: dict[str, object]) -> str:
    node = shutil.which("node")
    assert node is not None, "node is required to run upsert-task tool tests"

    harness = tmp_path / "create_task_harness"
    harness.mkdir(parents=True, exist_ok=True)
    (harness / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    (harness / "plugin.mjs").write_text(
        "function schemaBuilder() {\n"
        "  const schema = {};\n"
        "  schema.describe = () => schema;\n"
        "  schema.optional = () => schema;\n"
        "  schema.int = () => schema;\n"
        "  schema.min = () => schema;\n"
        "  schema.max = () => schema;\n"
        "  schema.array = () => schemaBuilder();\n"
        "  return schema;\n"
        "}\n"
        "export function tool(definition) { return definition; }\n"
        "tool.schema = {\n"
        "  string: () => schemaBuilder(),\n"
        "  enum: () => schemaBuilder(),\n"
        "  number: () => schemaBuilder(),\n"
        "  array: () => schemaBuilder(),\n"
        "};\n",
        encoding="utf-8",
    )
    (harness / "_run-python-tool.mjs").write_text(
        files("jri.core.agents")
        .joinpath("_run-python-tool.mjs")
        .read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    module_path = harness / "upsert-task.mjs"
    source = (
        files("jri.core.agents").joinpath("upsert-task.js").read_text(encoding="utf-8")
    )
    module_path.write_text(
        source.replace(
            'import { tool } from "@opencode-ai/plugin";',
            'import { tool } from "./plugin.mjs";',
            1,
        ),
        encoding="utf-8",
    )
    script = (
        "const modulePath = process.argv.at(-2);\n"
        "const payloadText = process.argv.at(-1);\n"
        "const mod = await import(modulePath);\n"
        "const result = await mod.default.execute(JSON.parse(payloadText));\n"
        "process.stdout.write(result);\n"
    )
    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "--eval",
            script,
            module_path.as_uri(),
            json.dumps(payload),
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "JRI_PYTHON": sys.executable},
    )
    return result.stdout


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
                "target_status": "todo",
            },
        }
    )


def test_packaged_schemas_are_available() -> None:
    assert files("jri.core.schemas").joinpath("task-metadata.json").is_file()
    assert files("jri.core.schemas").joinpath("state.json").is_file()
    assert files("jri.core.agents").joinpath("interrogator.md").is_file()
    assert files("jri.core.agents").joinpath("ralph.md").is_file()
    assert files("jri.core.agents").joinpath("_run-python-tool.mjs").is_file()
    assert files("jri.core.agents").joinpath("delete-task.js").is_file()
    assert files("jri.core.agents").joinpath("promote-tasks.js").is_file()
    assert files("jri.core.agents").joinpath("ralph-result.js").is_file()
    assert files("jri.core.agents").joinpath("rename-task.js").is_file()
    assert files("jri.core.agents").joinpath("upsert-task.js").is_file()


def test_upsert_task_tool_writes_parseable_draft_and_overwrites(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".jri" / "tasks").mkdir(parents=True)

    created = run_upsert_task_tool(
        repo,
        tmp_path,
        {
            "title": "Clarify scope",
            "body": "Draft the scope.\n",
            "assignee": "Ralph",
            "priority": 1,
            "depends_on": ["setup"],
        },
    )

    assert created == "created draft task: .jri/tasks/draft/clarify-scope.md"
    task_path = repo / ".jri" / "tasks" / "draft" / "clarify-scope.md"
    assert task_path.read_text(encoding="utf-8").startswith("---\n{\n")

    created_task = parse_task_file(task_path)
    assert created_task.slug == "clarify-scope"
    assert created_task.metadata.title == "Clarify scope"
    assert created_task.metadata.depends_on == ["setup"]
    assert created_task.metadata.acceptance_criteria == []
    assert created_task.body == "Draft the scope.\n"

    updated = run_upsert_task_tool(
        repo,
        tmp_path,
        {
            "title": "Clarify scope",
            "slug": "clarify-scope",
            "body": "Refined draft.\n",
            "assignee": "Human",
            "priority": 0,
            "depends_on": [],
            "acceptance_criteria": ["Scope is approved"],
        },
    )

    assert updated == "updated draft task: .jri/tasks/draft/clarify-scope.md"
    updated_task = parse_task_file(task_path)
    assert updated_task.metadata.assignee == "Human"
    assert updated_task.metadata.priority == 0
    assert updated_task.metadata.acceptance_criteria == ["Scope is approved"]
    assert updated_task.body == "Refined draft.\n"


def test_upsert_task_tool_rejects_invalid_slug(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(
        subprocess.CalledProcessError,
        match="returned non-zero exit status",
    ):
        run_upsert_task_tool(
            repo,
            tmp_path,
            {
                "title": "Clarify scope",
                "slug": "../escape",
                "body": "Draft the scope.\n",
                "assignee": "Ralph",
                "priority": 1,
            },
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
        run_upsert_task_tool(
            repo,
            tmp_path,
            {
                "title": "Clarify scope",
                "body": "Draft the scope.\n",
                "assignee": "Ralph",
                "priority": 1,
            },
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
        run_upsert_task_tool(
            repo,
            tmp_path,
            {
                "title": "Clarify scope",
                "body": "Draft the scope.\n",
                "assignee": "Ralph",
                "priority": 1,
            },
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
        run_upsert_task_tool(
            repo,
            tmp_path,
            {
                "title": "Clarify scope",
                "slug": "clarify-scope",
                "body": "Draft the scope.\n",
                "assignee": "Ralph",
                "priority": 1,
            },
        )

    assert outside.read_text(encoding="utf-8") == "outside\n"


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
