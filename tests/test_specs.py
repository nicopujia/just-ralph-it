from pathlib import Path

import pytest

from jri.core.ai import ToolCallFinished, ToolCallStarted, architect, functional_analyst
from jri.core.service import Service
from tests.doubles.openai import FakeClient, reply, response, streamed_reply
from tests.doubles.settings import build_settings
from tests.git import create_repository, run_git

FUNCTIONAL_PATCH = """\
diff --git a/.jri/specs/functional/behavior.md b/.jri/specs/functional/behavior.md
new file mode 100644
--- /dev/null
+++ b/.jri/specs/functional/behavior.md
@@ -0,0 +1 @@
+# Behavior
"""
ARCHITECTURE_PATCH = """\
diff --git a/.jri/specs/architecture/design.md b/.jri/specs/architecture/design.md
new file mode 100644
--- /dev/null
+++ b/.jri/specs/architecture/design.md
@@ -0,0 +1 @@
+# Design
"""
MISCOUNTED_FUNCTIONAL_PATCH = """\
diff --git a/.jri/specs/functional/behavior.md b/.jri/specs/functional/behavior.md
new file mode 100644
--- /dev/null
+++ b/.jri/specs/functional/behavior.md
@@ -0,0 +1,9 @@
+# Behavior
+Totals are supported.
"""
RENAME_PATCH = """\
diff --git a/.jri/specs/functional/behavior.md b/.jri/specs/functional/other.md
similarity index 100%
rename from .jri/specs/functional/behavior.md
rename to ../../../escape.md
"""
FUNCTIONAL_UPDATE = """\
diff --git a/.jri/specs/functional/behavior.md b/.jri/specs/functional/behavior.md
--- a/.jri/specs/functional/behavior.md
+++ b/.jri/specs/functional/behavior.md
@@ -1 +1,2 @@
 # Behavior
+Total output is supported.
"""
ARCHITECTURE_UPDATE = """\
diff --git a/.jri/specs/architecture/design.md b/.jri/specs/architecture/design.md
--- a/.jri/specs/architecture/design.md
+++ b/.jri/specs/architecture/design.md
@@ -1 +1,2 @@
 # Design
+Add a total accumulator.
"""


def build_service(path: Path, client: FakeClient) -> Service:
    Service.init(path)
    return Service(build_settings(path, client))


def successful_client() -> FakeClient:
    return FakeClient(
        [streamed_reply("Repository report"), response(reply("Specifications ready."))],
        parsed=[
            functional_analyst.Output(
                result=functional_analyst.Patch(outcome="specification_patch", patch=FUNCTIONAL_PATCH)
            ),
            architect.Output(result=architect.Patch(outcome="architecture_patch", patch=ARCHITECTURE_PATCH)),
        ],
    )


def written_specs() -> functional_analyst.Output:
    return functional_analyst.Output(
        result=functional_analyst.Patch(outcome="specification_patch", patch=FUNCTIONAL_PATCH)
    )


def reported_issues(*issues: str) -> architect.Output:
    return architect.Output(result=architect.Issues(outcome="functional_specification_issues", issues=list(issues)))


def collect_tool_calls(service: Service) -> list[tuple[str, str, str]]:
    return [
        (type(event).__name__, event.call_id, event.label)
        for event in service.ralph()
        if isinstance(event, ToolCallStarted | ToolCallFinished)
    ]


def test_commits_complete_specification_bundle(tmp_path: Path) -> None:
    create_repository(tmp_path)
    service = build_service(tmp_path, successful_client())

    list(service.ralph())

    assert (tmp_path / ".jri/specs/functional/behavior.md").read_text() == "# Behavior\n"
    assert (tmp_path / ".jri/specs/architecture/design.md").read_text() == "# Design\n"
    assert run_git(tmp_path, "show", "-s", "--format=%B") == (
        "jri: update specifications\n\nCo-authored-by: ralphpujia <ralph@pujia.ar>"
    )
    assert run_git(tmp_path, "show", "--format=", "--name-only").splitlines() == [
        ".jri/.gitignore",
        ".jri/config.yaml",
        ".jri/notebook.yaml",
        ".jri/specs/architecture/design.md",
        ".jri/specs/functional/behavior.md",
    ]
    assert not run_git(tmp_path, "status", "--short")


def test_commits_specifications_whose_patch_miscounts_its_hunk(tmp_path: Path) -> None:
    create_repository(tmp_path)
    client = FakeClient(
        [streamed_reply("Repository report"), response(reply("Specifications ready."))],
        parsed=[
            functional_analyst.Output(
                result=functional_analyst.Patch(outcome="specification_patch", patch=MISCOUNTED_FUNCTIONAL_PATCH)
            ),
            architect.Output(result=architect.Patch(outcome="architecture_patch", patch=ARCHITECTURE_PATCH)),
        ],
    )
    service = build_service(tmp_path, client)

    list(service.ralph())

    assert (tmp_path / ".jri/specs/functional/behavior.md").read_text() == "# Behavior\nTotals are supported.\n"
    assert service.session.active_spec_commit is not None


def test_returns_ambiguities_to_the_interviewer_without_committing(tmp_path: Path) -> None:
    create_repository(tmp_path)
    head = run_git(tmp_path, "rev-parse", "HEAD")
    ambiguity = "Choose whether output is JSON or plain text."
    client = FakeClient(
        [response(reply("Understood.")), response(reply("Should the output be JSON or plain text?"))],
        parsed=[
            functional_analyst.Output(
                result=functional_analyst.Ambiguities(outcome="ambiguities", ambiguities=[ambiguity])
            )
        ],
    )
    service = build_service(tmp_path, client)
    list(service.chat("Build a reporting CLI."))

    list(service.ralph())

    assert run_git(tmp_path, "rev-parse", "HEAD") == head
    assert not (tmp_path / ".jri/specs").exists()
    assert service.session.active_spec_commit is None
    assert any(ambiguity in item.get("content", "") for item in service.session.interview)
    restarted = build_service(tmp_path, FakeClient([]))
    turns, _ = restarted.restore()
    assert ("assistant", "Should the output be JSON or plain text?", None) in turns[-1].items
    assert restarted.session.active_spec_commit is None


def test_reports_one_row_per_polishing_round(tmp_path: Path) -> None:
    create_repository(tmp_path)
    client = FakeClient(
        [streamed_reply("Repository report"), response(reply("Specifications ready."))],
        parsed=[
            written_specs(),
            reported_issues("Undefined totals.", "Unclear export.", "Missing errors."),
            written_specs(),
            reported_issues("Unclear export."),
            written_specs(),
            reported_issues("Missing errors.", "Undefined limits."),
            written_specs(),
            architect.Output(result=architect.Patch(outcome="architecture_patch", patch=ARCHITECTURE_PATCH)),
        ],
    )
    service = build_service(tmp_path, client)

    events = collect_tool_calls(service)

    assert events == [
        ("ToolCallStarted", "functional", "Writing functional specifications from your project notes"),
        ("ToolCallFinished", "functional", "Wrote functional specifications from your project notes"),
        ("ToolCallStarted", "explorer", "Studying your existing project"),
        ("ToolCallFinished", "explorer", "Studied your existing project"),
        ("ToolCallStarted", "architecture", "Designing the project architecture"),
        ("ToolCallFinished", "architecture", "Drafted the project architecture"),
        ("ToolCallStarted", "polish-1", "3 issues found. Polishing... (round 1)"),
        ("ToolCallFinished", "polish-1", "3 issues found. Polishing... (round 1)"),
        ("ToolCallStarted", "polish-2", "1 issues found. Polishing... (round 2)"),
        ("ToolCallFinished", "polish-2", "1 issues found. Polishing... (round 2)"),
        ("ToolCallStarted", "polish-3", "2 issues found. Polishing... (round 3)"),
        ("ToolCallFinished", "polish-3", "2 issues found. Polishing... (round 3)"),
    ]
    assert service.session.active_spec_commit is not None


def test_finishes_the_open_polishing_round_when_ambiguities_appear(tmp_path: Path) -> None:
    create_repository(tmp_path)
    client = FakeClient(
        [streamed_reply("Repository report"), response(reply("Should the output be JSON or plain text?"))],
        parsed=[
            written_specs(),
            reported_issues("Unclear export.", "Missing errors."),
            functional_analyst.Output(
                result=functional_analyst.Ambiguities(outcome="ambiguities", ambiguities=["JSON or plain text?"])
            ),
        ],
    )
    service = build_service(tmp_path, client)

    events = collect_tool_calls(service)

    assert events[-2:] == [
        ("ToolCallStarted", "polish-1", "2 issues found. Polishing... (round 1)"),
        ("ToolCallFinished", "polish-1", "Found project details to clarify"),
    ]
    assert [call_id for kind, call_id, _ in events if kind == "ToolCallStarted"] == [
        call_id for kind, call_id, _ in events if kind == "ToolCallFinished"
    ]
    assert service.session.active_spec_commit is None


def test_updates_specs_after_restart_and_an_intervening_project_commit(tmp_path: Path) -> None:
    create_repository(tmp_path)
    service = build_service(tmp_path, successful_client())
    list(service.ralph())
    first_spec_commit = service.session.active_spec_commit
    assert first_spec_commit is not None

    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n")
    run_git(tmp_path, "add", "CHANGELOG.md")
    run_git(tmp_path, "commit", "-qm", "docs: add changelog")
    project_commit = run_git(tmp_path, "rev-parse", "HEAD")

    restarted = build_service(
        tmp_path,
        FakeClient(
            [streamed_reply("Updated repository report"), response(reply("Specifications updated."))],
            parsed=[
                functional_analyst.Output(
                    result=functional_analyst.Patch(outcome="specification_patch", patch=FUNCTIONAL_UPDATE)
                ),
                architect.Output(result=architect.Patch(outcome="architecture_patch", patch=ARCHITECTURE_UPDATE)),
            ],
        ),
    )
    restarted.restore()
    assert restarted.session.active_spec_commit == first_spec_commit
    restarted.interviewer.notebook.add(["Add a total output record."], "t1")

    list(restarted.ralph())

    second_spec_commit = restarted.session.active_spec_commit
    assert second_spec_commit is not None
    assert second_spec_commit != first_spec_commit
    run_git(tmp_path, "merge-base", "--is-ancestor", first_spec_commit, second_spec_commit)
    assert run_git(tmp_path, "rev-parse", f"{second_spec_commit}^") == project_commit
    assert run_git(tmp_path, "log", "-3", "--format=%s").splitlines() == [
        "jri: update specifications",
        "docs: add changelog",
        "jri: update specifications",
    ]
    assert changelog.read_text() == "# Changelog\n"
    assert (tmp_path / "README.md").read_text() == "# Project\n"
    assert (tmp_path / ".jri/specs/functional/behavior.md").read_text() == ("# Behavior\nTotal output is supported.\n")
    assert (tmp_path / ".jri/specs/architecture/design.md").read_text() == ("# Design\nAdd a total accumulator.\n")
    assert run_git(tmp_path, "show", "--format=", "--name-only", second_spec_commit).splitlines() == [
        ".jri/notebook.yaml",
        ".jri/specs/architecture/design.md",
        ".jri/specs/functional/behavior.md",
    ]
    reopened = build_service(tmp_path, FakeClient([]))
    reopened.restore()
    assert reopened.session.active_spec_commit == second_spec_commit
    assert not run_git(tmp_path, "status", "--short")


def test_commits_modified_configuration_with_specifications(tmp_path: Path) -> None:
    create_repository(tmp_path)
    service = build_service(tmp_path, successful_client())
    config = tmp_path / ".jri/config.yaml"
    run_git(tmp_path, "add", ".jri/config.yaml")
    run_git(tmp_path, "commit", "-qm", "add configuration")
    config.write_text(f"{config.read_text()}\n# Project-specific configuration.\n")

    list(service.ralph())

    assert run_git(tmp_path, "show", "HEAD:.jri/config.yaml").endswith("# Project-specific configuration.")
    assert ".jri/config.yaml" in run_git(tmp_path, "show", "--format=", "--name-only").splitlines()
    assert not run_git(tmp_path, "status", "--short")


def test_initializes_and_commits_new_repository(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# New project\n")
    service = build_service(tmp_path, successful_client())

    list(service.ralph())

    assert run_git(tmp_path, "show", "--format=", "--name-only").splitlines() == [
        ".jri/.gitignore",
        ".jri/config.yaml",
        ".jri/notebook.yaml",
        ".jri/specs/architecture/design.md",
        ".jri/specs/functional/behavior.md",
        "README.md",
    ]
    assert service.session.active_spec_commit == run_git(tmp_path, "rev-parse", "HEAD")
    assert not run_git(tmp_path, "status", "--short")


def test_refuses_unrelated_changes_before_generation(tmp_path: Path) -> None:
    create_repository(tmp_path)
    service = build_service(tmp_path, FakeClient([]))
    (tmp_path / "unrelated.txt").write_text("block")

    with pytest.raises(RuntimeError, match=r"unrelated\.txt"):
        list(service.ralph())

    assert run_git(tmp_path, "log", "--oneline").count("\n") == 0


def test_refuses_to_commit_when_the_project_moved_during_generation(tmp_path: Path) -> None:
    create_repository(tmp_path)
    service = build_service(tmp_path, successful_client())
    events = service.ralph()
    next(events)
    run_git(tmp_path, "commit", "--allow-empty", "-qm", "concurrent")

    with pytest.raises(RuntimeError, match="changed while specifications were being generated"):
        list(events)

    assert service.session.active_spec_commit is None


def test_refuses_to_commit_when_the_notebook_moved_during_generation(tmp_path: Path) -> None:
    create_repository(tmp_path)
    service = build_service(tmp_path, successful_client())
    events = service.ralph()
    next(events)
    service.interviewer.notebook.add(["Captured while generating."], "t1")

    with pytest.raises(RuntimeError, match="changed while specifications were being generated"):
        list(events)

    assert service.session.active_spec_commit is None


def test_refuses_a_project_file_renamed_onto_a_workspace_path(tmp_path: Path) -> None:
    create_repository(tmp_path)
    service = build_service(tmp_path, FakeClient([]))
    run_git(tmp_path, "mv", "-f", "README.md", ".jri/notebook.yaml")

    with pytest.raises(RuntimeError, match=r"README\.md"):
        list(service.ralph())

    assert run_git(tmp_path, "log", "--oneline").count("\n") == 0


def test_refuses_existing_specifications_without_an_active_commit(tmp_path: Path) -> None:
    create_repository(tmp_path)
    spec = tmp_path / ".jri/specs/functional/behavior.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# Behavior\n")
    run_git(tmp_path, "add", ".jri")
    run_git(tmp_path, "commit", "-qm", "add specifications")
    service = build_service(tmp_path, FakeClient([]))

    with pytest.raises(RuntimeError, match="no active JRI commit"):
        list(service.ralph())


def test_refuses_active_commit_missing_from_git(tmp_path: Path) -> None:
    service = build_service(tmp_path, FakeClient([]))
    service.update_session(active_spec_commit="0" * 40)

    with pytest.raises(RuntimeError, match="missing from Git"):
        list(service.ralph())


def test_refuses_active_commit_unreachable_from_head(tmp_path: Path) -> None:
    create_repository(tmp_path)
    initial = run_git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
    run_git(tmp_path, "add", "CHANGELOG.md")
    run_git(tmp_path, "commit", "-qm", "docs: add changelog")
    abandoned = run_git(tmp_path, "rev-parse", "HEAD")
    run_git(tmp_path, "reset", "-q", "--hard", initial)
    service = build_service(tmp_path, FakeClient([]))
    service.update_session(active_spec_commit=abandoned)

    with pytest.raises(RuntimeError, match="not reachable from HEAD"):
        list(service.ralph())


def test_refuses_specifications_edited_outside_jri(tmp_path: Path) -> None:
    create_repository(tmp_path)
    service = build_service(tmp_path, successful_client())
    list(service.ralph())
    (tmp_path / ".jri/specs/functional/behavior.md").write_text("# Behavior\nEdited by hand.\n")
    run_git(tmp_path, "add", ".jri/specs")
    run_git(tmp_path, "commit", "-qm", "docs: edit specifications")

    with pytest.raises(RuntimeError, match="differ from the active JRI commit"):
        list(service.ralph())


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        (
            FUNCTIONAL_PATCH.replace(
                "--- /dev/null\n+++ b/.jri/specs/functional/behavior.md", "--- a/README.md\n+++ b/README.md"
            ).replace("@@ -0,0 +1 @@", "@@ -1 +1 @@"),
            r"README\.md",
        ),
        (FUNCTIONAL_PATCH.replace("new file mode 100644", "index 0000000..e69de29 120000"), "modes or symlinks"),
        (FUNCTIONAL_PATCH.replace("new file mode 100644", "new file mode 100755"), "modes or symlinks"),
        (FUNCTIONAL_PATCH.replace("new file mode 100644", "old mode 100644\nnew mode 100755"), "modes or symlinks"),
        (FUNCTIONAL_PATCH.replace("+# Behavior", "GIT binary patch"), "binary files"),
        (FUNCTIONAL_PATCH.replace("+# Behavior", "Binary files a/x.md and b/x.md differ"), "binary files"),
        (
            FUNCTIONAL_PATCH.replace("functional/behavior.md", "functional/../../../escape.md"),
            r"cannot change `\.jri/specs/functional/\.\./\.\./\.\./escape\.md`",
        ),
        (
            FUNCTIONAL_PATCH.replace("behavior.md", "behavior.txt"),
            r"cannot change `\.jri/specs/functional/behavior\.txt`",
        ),
        (
            FUNCTIONAL_PATCH.replace(".jri/specs/functional/behavior.md", "/etc/escape.md"),
            r"cannot change `/etc/escape\.md`",
        ),
        (FUNCTIONAL_PATCH.replace("+++ b/.jri/specs/functional/behavior.md", "+++ behavior.md"), "Malformed"),
        (FUNCTIONAL_PATCH.replace("diff --git a/.jri", "diff --git .jri"), "Malformed"),
        (RENAME_PATCH, r"cannot change `\.\./\.\./\.\./escape\.md`"),
        ("", "at least one file"),
    ],
    ids=[
        "outside-tree",
        "symlink",
        "executable",
        "mode-change",
        "binary-hunk",
        "binary-summary",
        "traversal",
        "non-markdown",
        "absolute-path",
        "malformed-header",
        "malformed-diff-line",
        "rename-escape",
        "empty",
    ],
)
def test_refuses_unsafe_specification_patch(tmp_path: Path, patch: str, reason: str) -> None:
    create_repository(tmp_path)
    client = FakeClient(
        [],
        parsed=[functional_analyst.Output(result=functional_analyst.Patch(outcome="specification_patch", patch=patch))],
    )
    service = build_service(tmp_path, client)

    with pytest.raises(RuntimeError, match=reason):
        list(service.ralph())
