import subprocess
from pathlib import Path

from .errors import JriError

MSG_INIT = "jri: init"
MSG_UPGRADE = "jri: upgrade"
MSG_UPGRADE_AUTO = "jri: auto-upgrade"
MSG_START_BEGIN = "jri(start): begin {slug}"
MSG_START_COMPLETE = "jri(start): complete {slug}"
MSG_PROMOTE = "jri: move drafts to todo"
MSG_RECOVER_FAILED = "jri: recover {slug} after failed task"
MSG_RECOVER_STALE = "jri: recover {slug} after stale run"
MSG_RECOVER_NEEDS_HUMAN = "jri: recover {slug} for needs-human"
MSG_ESCALATE_HUMAN = "jri: escalate {slug} for human help"
MSG_RALPH_FINALIZE = "jri(ralph): finalize {slug}"
MSG_RALPH_PARTIAL = "jri(ralph): partial work on {slug}"


def tag_name(slug: str, suffix: str) -> str:
    """Generate a JRI tag name with begin/end suffix.

    Args:
        slug: The task slug
        suffix: Either 'begin' or 'end'

    Returns:
        Tag name in format jri/{suffix}/{slug}
    """
    return f"jri/{suffix}/{slug}"


def parse_tag_name(tag: str) -> tuple[str, str] | None:
    """Parse a JRI tag name into (suffix, slug) tuple.

    Args:
        tag: The tag name to parse

    Returns:
        Tuple of (suffix, slug) if it's a JRI tag, None otherwise.
        Suffix is either 'begin' or 'end'.
    """
    if not tag.startswith("jri/"):
        return None
    parts = tag.split("/")
    if len(parts) != 3:
        return None
    _, suffix, slug = parts
    if suffix not in ("begin", "end"):
        return None
    return (suffix, slug)


class GitRepo:
    def __init__(self, root: Path) -> None:
        self.root = root

    def relative_path(self, path: Path | str) -> str:
        candidate = Path(path)
        if candidate.is_absolute():
            try:
                candidate = candidate.relative_to(self.root)
            except ValueError:
                pass
        return candidate.as_posix()

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=check,
            capture_output=True,
            text=True,
        )

    def ensure_repo(self) -> None:
        if not self.is_repo():
            raise JriError("jri requires a git repository")

    def is_repo(self) -> bool:
        result = self.run("rev-parse", "--is-inside-work-tree", check=False)
        return result.returncode == 0 and result.stdout.strip() == "true"

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
            raise JriError(f"jri ctl start must begin from the {default} branch")

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

    def has_local_branch(self, name: str) -> bool:
        result = self.run(
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{name}",
            check=False,
        )
        return result.returncode == 0

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

    def path_matches_head(self, path: Path | str) -> bool:
        relative_path = self.relative_path(path)
        if not self.is_tracked(relative_path):
            return True
        result = self.run("diff", "--quiet", "HEAD", "--", relative_path, check=False)
        if result.returncode in (0, 1):
            return result.returncode == 0
        raise JriError(
            result.stderr.strip() or f"failed to diff HEAD for {relative_path}"
        )

    def merge_ff_only(self, branch: str) -> None:
        result = self.run("merge", "--ff-only", branch, check=False)
        if result.returncode != 0:
            raise JriError(result.stderr.strip() or f"failed to merge {branch}")

    def create_tag(self, name: str) -> None:
        result = self.run("tag", name, check=False)
        if result.returncode != 0:
            raise JriError(result.stderr.strip() or f"failed to create tag {name}")

    def has_tag(self, name: str) -> bool:
        result = self.run(
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/tags/{name}",
            check=False,
        )
        return result.returncode == 0

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self.run(
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            check=False,
        )
        if result.returncode in (0, 1):
            return result.returncode == 0
        raise JriError(
            result.stderr.strip()
            or f"failed to compare ancestry between {ancestor} and {descendant}"
        )

    def has_remote(self) -> bool:
        return bool(self.run("remote").stdout.strip())

    def push_task_refs(self, *, branch: str, tag: str) -> None:
        default = self.default_branch()
        for args in (
            ("push", "origin", default),
            ("push", "origin", branch),
            ("push", "origin", tag),
        ):
            result = self.run(*args, check=False)
            if result.returncode != 0:
                raise JriError(result.stderr.strip() or f"failed to {' '.join(args)}")

    def diff(self, from_ref: str, to_ref: str) -> str:
        result = self.run("diff", from_ref, to_ref, check=False)
        if result.returncode not in (0, 1):
            raise JriError(
                result.stderr.strip() or f"failed to diff {from_ref}..{to_ref}"
            )
        return result.stdout

    def reset_hard(self, ref: str) -> None:
        result = self.run("reset", "--hard", ref, check=False)
        if result.returncode != 0:
            raise JriError(result.stderr.strip() or f"failed to reset to {ref}")

    def reset_branch(self, branch: str, ref: str) -> None:
        result = self.run("update-ref", f"refs/heads/{branch}", ref, check=False)
        if result.returncode != 0:
            raise JriError(
                result.stderr.strip() or f"failed to reset branch {branch} to {ref}"
            )

    def add_worktree(self, path: Path, branch: str) -> None:
        result = self.run("worktree", "add", str(path), branch, check=False)
        if result.returncode != 0:
            raise JriError(
                result.stderr.strip() or f"failed to create worktree at {path}"
            )

    def remove_worktree(self, path: Path) -> None:
        result = self.run("worktree", "remove", str(path), "--force", check=False)
        if result.returncode != 0 and path.exists():
            raise JriError(
                result.stderr.strip() or f"failed to remove worktree at {path}"
            )

    def rev_parse(self, ref: str) -> str:
        result = self.run("rev-parse", ref, check=False)
        if result.returncode != 0:
            raise JriError(result.stderr.strip() or f"failed to resolve {ref}")
        return result.stdout.strip()
