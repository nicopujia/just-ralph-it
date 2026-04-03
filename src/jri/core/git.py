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

    def status_short(self, *paths: str) -> str:
        args = ["status", "--short"]
        if paths:
            args.extend(["--", *paths])
        return self.run(*args).stdout.strip()

    def ensure_clean(self) -> None:
        if self.status_short():
            raise JriError("git working tree must be clean")

    def current_branch(self) -> str:
        return self.run("branch", "--show-current").stdout.strip()

    def default_branch(self, *, hint: str | None = None) -> str:
        if hint:
            return hint
        result = self.run(
            "rev-parse", "--verify", "--quiet", "refs/heads/main", check=False
        )
        if result.returncode == 0:
            return "main"
        result = self.run(
            "rev-parse", "--verify", "--quiet", "refs/heads/master", check=False
        )
        if result.returncode == 0:
            return "master"
        return self.current_branch() or "main"

    def ensure_default_branch(self, *, hint: str | None = None) -> None:
        default = self.default_branch(hint=hint)
        if self.current_branch() != default:
            raise JriError(f"jri start must begin from the {default} branch")

    def checkout_new_branch(self, name: str) -> None:
        result = self.run("checkout", "-b", name, check=False)
        if result.returncode != 0:
            raise JriError(result.stderr.strip() or f"failed to create branch {name}")

    def checkout(self, name: str) -> None:
        result = self.run("checkout", name, check=False)
        if result.returncode != 0:
            raise JriError(result.stderr.strip() or f"failed to checkout {name}")

    def delete_branch(self, name: str) -> None:
        self.run("branch", "-D", name, check=False)

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

    def commit_paths_if_needed(self, message: str, paths: list[str]) -> bool:
        scoped_paths = list(dict.fromkeys(paths))
        if not self.status_short(*scoped_paths):
            return False

        self.run("add", "-A", "--", *scoped_paths)
        result = self.run("commit", "-m", message, "--", *scoped_paths, check=False)
        if result.returncode != 0:
            raise JriError(result.stderr.strip() or f"failed to commit: {message}")
        return True

    def commit_upgrade_if_needed(
        self,
        message: str,
        *,
        managed_paths: list[str],
        untracked_paths: list[str],
    ) -> bool:
        scoped_paths = list(dict.fromkeys([*managed_paths, *untracked_paths]))
        if not self.status_short(*scoped_paths):
            return False

        tracked_paths = [path for path in untracked_paths if self.is_tracked(path)]
        if tracked_paths:
            stashed = self.run("stash", "push", "--staged", "--quiet", check=False)
            self.run("add", "-A", "--", *managed_paths)
            self.run("rm", "--cached", "--quiet", "--", *tracked_paths)
            result = self.run("commit", "-m", message, check=False)
            if stashed.returncode == 0:
                self.run("stash", "pop", "--quiet", check=False)
        else:
            self.run("add", "-A", "--", *managed_paths)
            result = self.run(
                "commit", "-m", message, "--", *managed_paths, check=False
            )
        if result.returncode != 0:
            raise JriError(result.stderr.strip() or f"failed to commit: {message}")
        return True

    def is_tracked(self, path: str) -> bool:
        result = self.run("ls-files", "--error-unmatch", "--", path, check=False)
        return result.returncode == 0

    def merge_ff_only(self, branch: str) -> None:
        result = self.run("merge", "--ff-only", branch, check=False)
        if result.returncode != 0:
            raise JriError(result.stderr.strip() or f"failed to merge {branch}")

    def create_tag(self, name: str) -> None:
        result = self.run("tag", name, check=False)
        if result.returncode != 0:
            raise JriError(result.stderr.strip() or f"failed to create tag {name}")

    def has_tag(self, name: str) -> bool:
        result = self.run("rev-parse", "--verify", "--quiet", f"refs/tags/{name}", check=False)
        return result.returncode == 0

    def has_remote(self) -> bool:
        return bool(self.run("remote").stdout.strip())

    def push_iteration(self, *, branch: str, tag: str) -> None:
        default = self.default_branch()
        for args in (
            ("push", "origin", default),
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
