import subprocess
from pathlib import Path

from .errors import JriError


class GitRepo:
    def __init__(self, root: Path) -> None:
        self.root = root

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=check,
            capture_output=True,
            text=True,
        )

    def ensure_repo(self) -> None:
        result = self.run("rev-parse", "--is-inside-work-tree", check=False)
        if result.returncode != 0 or result.stdout.strip() != "true":
            raise JriError("jri requires a git repository")

    def status_short(self) -> str:
        return self.run("status", "--short").stdout.strip()

    def ensure_clean(self) -> None:
        if self.status_short():
            raise JriError("git working tree must be clean")

    def current_branch(self) -> str:
        return self.run("branch", "--show-current").stdout.strip()

    def ensure_main(self) -> None:
        if self.current_branch() != "main":
            raise JriError("jri start must begin from a clean main branch")

    def checkout_new_branch(self, name: str) -> None:
        result = self.run("checkout", "-b", name, check=False)
        if result.returncode != 0:
            raise JriError(result.stderr.strip() or f"failed to create branch {name}")

    def checkout(self, name: str) -> None:
        result = self.run("checkout", name, check=False)
        if result.returncode != 0:
            raise JriError(result.stderr.strip() or f"failed to checkout {name}")

    def add_all(self) -> None:
        self.run("add", "-A")

    def commit(self, message: str) -> None:
        result = self.run("commit", "-m", message, check=False)
        if result.returncode != 0:
            raise JriError(result.stderr.strip() or f"failed to commit: {message}")

    def commit_all_if_needed(self, message: str) -> bool:
        if not self.status_short():
            return False
        self.add_all()
        self.commit(message)
        return True

    def merge_ff_only(self, branch: str) -> None:
        result = self.run("merge", "--ff-only", branch, check=False)
        if result.returncode != 0:
            raise JriError(result.stderr.strip() or f"failed to merge {branch}")

    def create_tag(self, name: str) -> None:
        result = self.run("tag", name, check=False)
        if result.returncode != 0:
            raise JriError(result.stderr.strip() or f"failed to create tag {name}")

    def has_remote(self) -> bool:
        return bool(self.run("remote").stdout.strip())

    def push_iteration(self, *, branch: str, tag: str) -> None:
        for args in (
            ("push", "origin", "main"),
            ("push", "origin", branch),
            ("push", "origin", tag),
        ):
            result = self.run(*args, check=False)
            if result.returncode != 0:
                raise JriError(result.stderr.strip() or f"failed to {' '.join(args)}")

    def reset_hard(self, ref: str) -> None:
        result = self.run("reset", "--hard", ref, check=False)
        if result.returncode != 0:
            raise JriError(result.stderr.strip() or f"failed to reset to {ref}")
