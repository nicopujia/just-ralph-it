import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from jri.core.ai import architect, functional_analyst
from jri.core.service import Service
from tests.doubles.openai import FakeClient, reply, response, streamed_reply

if TYPE_CHECKING:
    from jri.core.settings import Settings


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


class FakeSettings(SimpleNamespace):
    def model_copy(self, *, update: dict[str, object]) -> "FakeSettings":
        return FakeSettings(**(vars(self) | update))


def run_git(path: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    assert executable is not None
    return subprocess.run(
        [executable, "-C", str(path), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


def build_service(path: Path, client: FakeClient) -> Service:
    settings = FakeSettings(
        cwd=path,
        force=False,
        logging_level="CRITICAL",
        llm_client=client,
        interviewer_model="test",
        interviewer_reasoning_effort=None,
        interviewer_temperature=0,
        explorer_model="test",
        explorer_reasoning_effort=None,
        explorer_temperature=0,
    )
    return Service(cast("Settings", settings))


def create_repository(path: Path) -> None:
    run_git(path, "init", "-q")
    run_git(path, "config", "user.name", "Test User")
    run_git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text("# Project\n")
    run_git(path, "add", "README.md")
    run_git(path, "commit", "-qm", "initial")


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
        ".jri/notebook.yaml",
        ".jri/specs/architecture/design.md",
        ".jri/specs/functional/behavior.md",
    ]
    assert not run_git(tmp_path, "status", "--short")


def test_initializes_and_commits_new_repository(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# New project\n")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test User")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test User")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    service = build_service(tmp_path, successful_client())

    list(service.ralph())

    assert run_git(tmp_path, "show", "--format=", "--name-only").splitlines() == [
        ".jri/.gitignore",
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


def test_refuses_patch_headers_outside_specification_tree(tmp_path: Path) -> None:
    create_repository(tmp_path)
    unsafe_patch = FUNCTIONAL_PATCH.replace(
        "--- /dev/null\n+++ b/.jri/specs/functional/behavior.md", "--- a/README.md\n+++ b/README.md"
    ).replace("@@ -0,0 +1 @@", "@@ -1 +1 @@")
    client = FakeClient(
        [],
        parsed=[
            functional_analyst.Output(
                result=functional_analyst.Patch(outcome="specification_patch", patch=unsafe_patch)
            )
        ],
    )
    service = build_service(tmp_path, client)

    with pytest.raises(RuntimeError, match=r"README\.md"):
        list(service.ralph())

    assert (tmp_path / "README.md").read_text() == "# Project\n"
