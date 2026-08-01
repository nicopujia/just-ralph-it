"""Small subprocess-backed Git interface."""

import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Error", "NotInstalledError", "NotRepositoryError", "Repository", "Status", "find_root"]
logger = logging.getLogger(__name__)


class Error(RuntimeError):
    """Raised when a Git command fails."""


class NotInstalledError(Error):
    """Raised when the Git executable is unavailable."""


class NotRepositoryError(Error):
    """Raised when a path is not inside a Git worktree."""


@dataclass(frozen=True)
class Status:
    """A path reported by Git's porcelain status."""

    path: str
    index: str
    worktree: str
    original_path: str | None = None


def find_root(path: Path) -> Path | None:
    """Find the worktree a path belongs to, without creating one.

    Returns:
        The worktree root, or ``None`` outside any repository.
    """

    executable = shutil.which("git")
    if executable is None:
        return None
    result = subprocess.run(
        [executable, "-C", str(path), "rev-parse", "--show-toplevel"], check=False, capture_output=True
    )
    return Path(os.fsdecode(result.stdout).strip()).resolve() if not result.returncode else None


class Repository:
    """Run reusable Git operations against a worktree."""

    def __init__(self, path: Path | str, executable: str = "git") -> None:
        resolved_executable = shutil.which(executable)
        if resolved_executable is None:
            raise NotInstalledError(f"Git executable not found: {executable}")
        self.executable = Path(resolved_executable)
        candidate = Path(path).resolve()
        result = subprocess.run(
            [str(self.executable), "-C", str(candidate), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
        )
        if result.returncode:
            candidate.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [str(self.executable), "-C", str(candidate), "init", "--quiet"], check=False, capture_output=True
            )
            if result.returncode:
                raise NotRepositoryError(os.fsdecode(result.stderr).strip() or f"Cannot initialize Git: {candidate}")
            self.path = candidate
        else:
            self.path = Path(os.fsdecode(result.stdout).strip()).resolve()

    def has_head(self) -> bool:
        """Return whether the repository has at least one commit.

        Returns:
            Whether ``HEAD`` resolves to a commit.
        """

        return self._run("rev-parse", "--verify", "HEAD", check=False).returncode == 0

    def read_head(self) -> str:
        """Return the current commit ID.

        Returns:
            The full commit ID at ``HEAD``.
        """

        return os.fsdecode(self._run("rev-parse", "HEAD").stdout).strip()

    def read_status(self) -> tuple[Status, ...]:
        """Return staged, unstaged, and untracked paths.

        Returns:
            Porcelain status entries for changed paths.
        """

        records = self._run("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout.split(b"\0")
        entries: list[Status] = []
        position = 0
        while position < len(records) and records[position]:
            record = records[position]
            index, worktree = chr(record[0]), chr(record[1])
            path = os.fsdecode(record[3:])
            original_path = None
            if index in {"R", "C"} or worktree in {"R", "C"}:
                position += 1
                original_path = os.fsdecode(records[position])
            entries.append(Status(path, index, worktree, original_path))
            position += 1
        return tuple(entries)

    def is_ancestor(self, ancestor: str, descendant: str = "HEAD") -> bool:
        """Return whether one revision is an ancestor of another.

        Returns:
            Whether ``ancestor`` is reachable from ``descendant``.
        """

        result = self._run("merge-base", "--is-ancestor", ancestor, descendant, check=False)
        if result.returncode not in {0, 1}:
            self._raise(result)
        return result.returncode == 0

    def read_file(self, revision: str, path: str) -> bytes:
        """Read a file from a committed revision.

        Returns:
            The committed file contents.
        """

        return self._run("show", f"{revision}:{path}").stdout

    def read_tree(self, revision: str, path: str = "") -> dict[str, bytes]:
        """Read every committed file below a path.

        Returns:
            Repository-relative paths mapped to their contents.
        """

        command = ["ls-tree", "-r", "-z", "--name-only", revision]
        if path:
            command.extend(["--", path])
        names = [os.fsdecode(name) for name in self._run(*command).stdout.split(b"\0") if name]
        return {name: self.read_file(revision, name) for name in names}

    def diff(self, base: str | None, *, paths: Sequence[str] = ()) -> bytes:
        """Return a revision or working-tree diff.

        Returns:
            The unified diff emitted by Git.
        """

        command = ["diff", base] if base is not None else ["diff", "--cached"]
        if paths:
            command.extend(["--", *paths])
        return self._run(*command).stdout

    def read_tracked_paths(self, revision: str = "HEAD") -> tuple[str, ...]:
        """Return paths tracked by a revision.

        Returns:
            Repository-relative tracked paths.
        """

        output = self._run("ls-tree", "-r", "-z", "--name-only", revision).stdout
        return tuple(os.fsdecode(path) for path in output.split(b"\0") if path)

    def read_worktree_paths(self) -> tuple[str, ...]:
        """Return tracked and unignored untracked worktree paths.

        Returns:
            Repository-relative paths visible to Git.
        """

        output = self._run("ls-files", "-co", "--exclude-standard", "-z").stdout
        return tuple(os.fsdecode(path) for path in output.split(b"\0") if path)

    @contextmanager
    def open_worktree(self, revision: str | None = "HEAD") -> Generator["Repository"]:
        """Create a temporary detached worktree.

        Yields:
            A repository rooted at the temporary worktree.
        """

        with tempfile.TemporaryDirectory(prefix="git-worktree-") as temporary_directory:
            location = Path(temporary_directory) / (revision or "worktree")
            if revision is None:
                location.mkdir()
                for relative_path in self.read_worktree_paths():
                    source = self.path / relative_path
                    if source.is_file():
                        destination = location / relative_path
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, destination)
                repository = Repository(location, str(self.executable))
                repository.stage((".",))
                yield repository
                return
            # A killed process cannot clean up after itself, so drop
            # the entries it left behind. Git only reclaims an entry
            # once its directory is gone from disk.
            self._run("worktree", "prune", check=False)
            self._run("worktree", "add", "--detach", str(location), revision)
            try:
                yield Repository(location, str(self.executable))
            finally:
                removal = self._run("worktree", "remove", "--force", str(location), check=False)
                if removal.returncode:
                    logger.warning("worktree_removal_failed location=%s", location)

    def apply_patch(self, patch: bytes, *, index: bool = False) -> None:
        """Apply a patch to the worktree."""

        arguments = ["apply"]
        if index:
            arguments.append("--index")
        self._run(*arguments, stdin=patch)

    def stage(self, paths: Sequence[str]) -> None:
        """Stage the given paths."""

        self._run("add", "--", *paths)

    def commit(self, message: str) -> str:
        """Create a commit with an exact message.

        Returns:
            The new commit ID.
        """

        self._run("commit", "--file=-", stdin=message.encode())
        return self.read_head()

    def _run(
        self, *arguments: str, stdin: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            [str(self.executable), "-C", str(self.path), *arguments], input=stdin, check=False, capture_output=True
        )
        if check and result.returncode:
            self._raise(result)
        return result

    @staticmethod
    def _raise(result: subprocess.CompletedProcess[bytes]) -> None:
        message = os.fsdecode(result.stderr).strip() or os.fsdecode(result.stdout).strip()
        raise Error(message or "Git command failed.")
