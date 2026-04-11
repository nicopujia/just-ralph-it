import subprocess
from importlib.resources import files
from pathlib import Path

import pytest

import jri.core.git as git_module
import jri.core.service as service_module
from tests.conftest import run_cli
from tests.helpers import git


def test_init_creates_scaffold_and_commit(git_repo: Path) -> None:
    exit_code = run_cli(["ctl", "init"], cwd=git_repo)

    assert exit_code == 0
    assert (git_repo / "README.md").read_text(encoding="utf-8") == "# temp repo\n"
    assert (
        (git_repo / ".jri" / "README.md")
        .read_text(encoding="utf-8")
        .startswith("# Runtime Files\n")
    )
    assert (git_repo / ".jri" / "learnings.md").read_text(encoding="utf-8") == ""
    assert (git_repo / ".jri" / "tasks" / "draft" / ".gitkeep").exists()
    assert (git_repo / ".jri" / "tasks" / "todo" / ".gitkeep").exists()
    assert (git_repo / ".jri" / "tasks" / "doing" / ".gitkeep").exists()
    assert (git_repo / ".jri" / "tasks" / "done" / ".gitkeep").exists()
    assert (git_repo / ".jri" / "attempts" / ".gitkeep").exists()
    assert (git_repo / "Makefile").read_text(encoding="utf-8") == (
        ".PHONY: check\n\n"
        "check:\n"
        '\t@echo "make check is not configured yet"\n'
        "\t@false\n"
    )
    assert not (git_repo / ".jri" / "signals").exists()
    assert not (git_repo / ".jri" / "logs").exists()
    assert not (git_repo / ".jri" / "worktree").exists()
    assert (git_repo / ".jri" / "state.json").exists()
    assert not (git_repo / ".opencode").exists()
    assert not (git_repo / "opencode.json").exists()
    assert not (git_repo / ".gitignore").exists()
    for name in service_module._MANAGED_AGENT_FILENAMES:
        assert (git_repo / ".jri" / ".opencode" / "agents" / name).exists()
    for name in service_module._MANAGED_TOOL_FILENAMES:
        assert (git_repo / ".jri" / ".opencode" / "tools" / name).exists()
    for name in service_module._MANAGED_CONFIG_FILENAMES:
        assert (git_repo / name).exists()
    assert (git_repo / ".jri" / "opencode.json").read_text(encoding="utf-8") == (
        files("jri.core.template").joinpath("opencode.json").read_text(encoding="utf-8")
    )
    assert git(git_repo, "log", "-1", "--pretty=%s") == git_module.MSG_INIT
    changed_files = set(
        git(
            git_repo,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "HEAD",
        ).splitlines()
    )
    assert set(service_module._MANAGED_AGENT_PATHS).issubset(changed_files)
    assert set(service_module._MANAGED_TOOL_PATHS).issubset(changed_files)
    assert set(service_module._MANAGED_CONFIG_FILENAMES).issubset(changed_files)
    assert "Makefile" in changed_files
    assert ".jri/.opencode/.gitignore" not in changed_files
    assert (git_repo / ".jri" / ".gitignore").read_text(
        encoding="utf-8"
    ).splitlines() == [
        "logs/",
        "signals/",
        "*state.json*",
        "metrics.json",
        "worktree/",
    ]
    assert git(git_repo, "status", "--short") == ""

    check = subprocess.run(
        ["make", "check"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode != 0
    assert "make check is not configured yet" in check.stdout


def test_init_commits_only_scaffold_when_unrelated_changes_exist(
    git_repo: Path,
) -> None:
    (git_repo / "README.md").write_text("# changed\n", encoding="utf-8")
    (git_repo / "notes.txt").write_text("keep staged\n", encoding="utf-8")
    git(git_repo, "add", "notes.txt")

    exit_code = run_cli(["ctl", "init"], cwd=git_repo)

    assert exit_code == 0
    changed_files = set(
        git(
            git_repo,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "HEAD",
        ).splitlines()
    )
    assert "README.md" not in changed_files
    assert "notes.txt" not in changed_files
    assert "Makefile" in changed_files
    assert ".jri/.gitignore" in changed_files
    assert ".gitignore" not in changed_files
    assert "README.md" in git(git_repo, "diff", "--name-only").splitlines()
    assert "notes.txt" in git(git_repo, "diff", "--cached", "--name-only").splitlines()


def test_init_creates_empty_readme_when_missing(git_repo: Path) -> None:
    (git_repo / "README.md").unlink()
    git(git_repo, "add", "README.md")
    git(git_repo, "commit", "-m", "remove readme")

    exit_code = run_cli(["ctl", "init"], cwd=git_repo)

    assert exit_code == 0
    assert (git_repo / "README.md").read_text(encoding="utf-8") == ""
    assert (git_repo / ".jri" / "README.md").exists()
    assert (git_repo / ".jri" / "learnings.md").read_text(encoding="utf-8") == ""
    changed_files = set(
        git(
            git_repo,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "HEAD",
        ).splitlines()
    )
    assert "README.md" in changed_files
    assert ".jri/learnings.md" in changed_files


def test_init_prompts_on_existing_dirs_and_aborts_when_no_input(git_repo: Path) -> None:
    """When run non-interactively (no stdin), init should abort when dirs exist."""
    assert run_cli(["ctl", "init"], cwd=git_repo) == 0

    # Second init without force should abort when there's no input
    exit_code = run_cli(["ctl", "init"], cwd=git_repo)

    # In non-interactive mode (EOFError), it should abort with exit code 1
    assert exit_code == 1


def test_init_delete_recreates_structure(git_repo: Path) -> None:
    assert run_cli(["ctl", "init"], cwd=git_repo) == 0
    extra = git_repo / ".jri" / "tasks" / "todo" / "extra.md"
    extra.write_text("temporary", encoding="utf-8")

    exit_code = run_cli(["ctl", "init", "--delete"], cwd=git_repo)

    assert exit_code == 0
    assert not extra.exists()


def test_init_force_removes_jri_opencode_dir(git_repo: Path) -> None:
    """Force flag should also remove .jri/.opencode/ directory."""
    assert run_cli(["ctl", "init"], cwd=git_repo) == 0

    custom_file = git_repo / ".jri" / ".opencode" / "custom.txt"
    custom_file.write_text("custom content", encoding="utf-8")

    exit_code = run_cli(["ctl", "init", "--force"], cwd=git_repo)

    assert exit_code == 0
    assert not custom_file.exists()
    assert (git_repo / ".jri" / ".opencode" / "agents" / "interrogator.md").exists()


def test_init_upgrade_refreshes_managed_files_without_deleting_tasks(
    git_repo: Path,
    monkeypatch,
) -> None:
    assert run_cli(["ctl", "init"], cwd=git_repo) == 0
    extra = git_repo / ".jri" / "tasks" / "todo" / "extra.md"
    extra.write_text("temporary task", encoding="utf-8")

    def fake_load_managed_template(name: str) -> str:
        base = name.removesuffix(".md") if name.endswith(".md") else name
        return f"upgraded {base}\n"

    monkeypatch.setattr(
        service_module, "_load_managed_template", fake_load_managed_template
    )

    exit_code = run_cli(["ctl", "init", "--upgrade"], cwd=git_repo)

    assert exit_code == 0
    assert extra.exists()
    assert git(git_repo, "log", "-1", "--pretty=%s") == git_module.MSG_UPGRADE


def test_init_upgrade_removes_legacy_root_managed_opencode_files(
    git_repo: Path,
) -> None:
    assert run_cli(["ctl", "init"], cwd=git_repo) == 0

    legacy_agent = git_repo / ".opencode" / "agents" / "interrogator.md"
    legacy_tool = git_repo / ".opencode" / "tools" / "upsert-task.js"
    legacy_config = git_repo / "opencode.json"
    legacy_agent.parent.mkdir(parents=True, exist_ok=True)
    legacy_tool.parent.mkdir(parents=True, exist_ok=True)
    legacy_agent.write_text("legacy agent\n", encoding="utf-8")
    legacy_tool.write_text("legacy tool\n", encoding="utf-8")
    legacy_config.write_text("{}\n", encoding="utf-8")

    assert run_cli(["ctl", "init", "--upgrade"], cwd=git_repo) == 0

    assert not legacy_agent.exists()
    assert not legacy_tool.exists()
    assert not legacy_config.exists()
    assert (git_repo / ".jri" / ".opencode" / "agents" / "interrogator.md").exists()
    assert (git_repo / ".jri" / "opencode.json").exists()


def test_init_upgrade_restores_missing_template_scaffold_files(git_repo: Path) -> None:
    assert run_cli(["ctl", "init"], cwd=git_repo) == 0
    git_repo.joinpath(".jri", "tasks", "draft", ".gitkeep").unlink()
    git_repo.joinpath(".jri", "attempts", ".gitkeep").unlink()
    git_repo.joinpath(".jri", "README.md").unlink()
    git_repo.joinpath(".jri", "learnings.md").unlink()

    assert run_cli(["ctl", "init", "--upgrade"], cwd=git_repo) == 0

    assert (git_repo / ".jri" / "tasks" / "draft" / ".gitkeep").exists()
    assert (git_repo / ".jri" / "attempts" / ".gitkeep").exists()
    assert (
        (git_repo / ".jri" / "README.md")
        .read_text(encoding="utf-8")
        .startswith("# Runtime Files\n")
    )
    assert (git_repo / ".jri" / "learnings.md").read_text(encoding="utf-8") == ""


def test_init_prompt_upgrade_refreshes_managed_files(
    git_repo: Path,
    monkeypatch,
) -> None:
    assert run_cli(["ctl", "init"], cwd=git_repo) == 0
    extra = git_repo / ".jri" / "tasks" / "todo" / "prompt-upgrade.md"
    extra.write_text("keep me", encoding="utf-8")

    def fake_load_managed_template(name: str) -> str:
        base = name.removesuffix(".md") if name.endswith(".md") else name
        return f"prompt-upgraded {base}\n"

    monkeypatch.setattr(
        service_module, "_load_managed_template", fake_load_managed_template
    )
    monkeypatch.setattr("builtins.input", lambda: "u")

    exit_code = run_cli(["ctl", "init"], cwd=git_repo)

    assert exit_code == 0
    assert extra.exists()
    assert git(git_repo, "log", "-1", "--pretty=%s") == git_module.MSG_UPGRADE


@pytest.mark.parametrize(
    "args",
    [["upgrade"], ["ctl", "init", "--bogus"]],
)
def test_init_rejects_removed_upgrade_command_and_unknown_flags(
    git_repo: Path,
    args: list[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run_cli(args, cwd=git_repo)

    assert exc_info.value.code == 2
