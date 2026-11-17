import os
from pathlib import Path

import pytest

from jri.core.repository import Repository
from tests.conftest import CreateRepository, RunGit


def test_hides_an_excluded_path_from_this_clone_alone(tmp_path: Path) -> None:
    repository = Repository.init(tmp_path)
    exclude_file = tmp_path / ".git" / "info" / "exclude"
    exclude_file.write_text("# A rule the user wrote", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("note\n", encoding="utf-8")

    repository.exclude("/notes.txt")
    repository.exclude("/notes.txt")

    assert not repository.read_status()
    assert exclude_file.read_text(encoding="utf-8") == "# A rule the user wrote\n/notes.txt\n"


def test_excludes_a_path_where_git_holds_no_exclude_file(tmp_path: Path) -> None:
    repository = Repository.init(tmp_path)
    (tmp_path / ".git" / "info" / "exclude").unlink()
    (tmp_path / ".git" / "info").rmdir()
    (tmp_path / "notes.txt").write_text("note\n", encoding="utf-8")

    repository.exclude("/notes.txt")

    assert not repository.read_status()
    assert (tmp_path / ".git" / "info" / "exclude").read_bytes() == b"/notes.txt\n"


def test_excludes_a_path_beside_a_rule_that_is_not_text(tmp_path: Path) -> None:
    repository = Repository.init(tmp_path)
    exclude_file = tmp_path / ".git" / "info" / "exclude"
    exclude_file.write_bytes(b"caf\xe9\n")

    repository.exclude("/notes.txt")

    assert exclude_file.read_bytes() == b"caf\xe9\n/notes.txt\n"


# A tree needs no author. A run writes this tree in a project where Git can name nobody.
def test_writes_the_tree_of_the_index_with_no_git_identity(
    tmp_path: Path, run_git: RunGit, monkeypatch: pytest.MonkeyPatch
) -> None:
    for variable in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(variable, raising=False)
    empty_config = tmp_path / "empty-config"
    empty_config.write_bytes(b"")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    project = tmp_path / "project"
    repository = Repository.init(project)
    run_git(project, "config", "user.useConfigOnly", "true")
    (project / "staged.md").write_bytes(b"staged\n")
    (project / "unstaged.md").write_bytes(b"unstaged\n")
    repository.stage(["staged.md"])
    (project / "staged.md").write_bytes(b"changed after the stage\n")

    tree = repository.write_tree()

    assert repository.read_tree(tree) == {"staged.md": b"staged\n"}
    assert not repository.has_commit()


# A private copy reads these settings before the global settings of the user.
def test_isolates_a_repository_from_the_git_settings_of_the_user(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    create_repository(tmp_path)
    repository = Repository(tmp_path)

    repository.isolate()

    settings = {
        name: run_git(tmp_path, "config", "--local", "--get", name)
        for name in ("apply.whitespace", "core.attributesFile", "core.autocrlf", "diff.context")
    }
    assert settings == {
        "apply.whitespace": "nowarn",
        "core.attributesFile": os.devnull,
        "core.autocrlf": "false",
        "diff.context": "3",
    }
