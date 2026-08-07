import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Self

__all__ = ["Error", "NotInstalledError", "NotRepositoryError", "Repository", "Status", "find_root"]

logger = logging.getLogger(__name__)


def find_root(path: Path) -> Path | None:
    executable = shutil.which("git")
    if executable is None:
        return None
    result = subprocess.run(
        [executable, "-C", str(path), "rev-parse", "--show-toplevel"], check=False, capture_output=True
    )
    return Path(os.fsdecode(result.stdout).strip()).resolve() if not result.returncode else None


class Error(RuntimeError): ...


class NotInstalledError(Error): ...


class NotRepositoryError(Error): ...


@dataclass(frozen=True)
class Status:
    path: str
    index: str
    worktree: str
    original_path: str | None = None


class Repository:
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
            raise NotRepositoryError(os.fsdecode(result.stderr).strip() or f"Not a Git worktree: {candidate}")
        self.path = Path(os.fsdecode(result.stdout).strip()).resolve()

    @classmethod
    def init(cls, path: Path | str, executable: str = "git") -> Self:
        if find_root(Path(path)) is not None:
            return cls(path, executable)
        resolved_executable = shutil.which(executable)
        if resolved_executable is None:
            raise NotInstalledError(f"Git executable not found: {executable}")
        candidate = Path(path).resolve()
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise NotRepositoryError(f"Cannot initialize Git: {candidate}") from error
        result = subprocess.run(
            [resolved_executable, "-C", str(candidate), "init", "--quiet"], check=False, capture_output=True
        )
        if result.returncode:
            raise NotRepositoryError(os.fsdecode(result.stderr).strip() or f"Cannot initialize Git: {candidate}")
        return cls(candidate, executable)

    def has_commit(self, revision: str = "HEAD") -> bool:
        arguments = ("rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
        return self._run(*arguments, check=False).returncode == 0

    def has_conflicts(self) -> bool:
        return bool(self._run("ls-files", "--unmerged", "-z").stdout)

    def is_on_branch(self) -> bool:
        # An unborn branch counts: HEAD names it before any commit
        # exists, and that is where a first commit would land.
        return self._run("symbolic-ref", "--quiet", "HEAD", check=False).returncode == 0

    def read_head(self) -> str:
        return os.fsdecode(self._run("rev-parse", "HEAD").stdout).strip()

    def read_status(self, paths: Sequence[str] = (), *, ignored: bool = False) -> tuple[Status, ...]:
        command = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
        if ignored:
            command.append("--ignored")
        if paths:
            command.extend(["--", *paths])
        records = self._run(*command).stdout.split(b"\0")
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

    def find_commit(self, text: str, revision: str = "HEAD") -> str | None:
        arguments = ("log", "--max-count=1", "--format=%H", "--fixed-strings", f"--grep={text}", revision)
        return os.fsdecode(self._run(*arguments).stdout).strip() or None

    def read_file(self, revision: str, path: str) -> bytes:
        return self._run("show", f"{revision}:{path}").stdout

    def read_tree(self, revision: str, path: str = "") -> dict[str, bytes]:
        command = ["ls-tree", "-r", "-z", "--name-only", revision]
        if path:
            command.extend(["--", path])
        names = [os.fsdecode(name) for name in self._run(*command).stdout.split(b"\0") if name]
        return {name: self.read_file(revision, name) for name in names}

    def diff(self, base: str | None, *, paths: Sequence[str] = ()) -> bytes:
        command = ["diff", base] if base is not None else ["diff", "--cached"]
        if paths:
            command.extend(["--", *paths])
        return self._run(*command).stdout

    def read_staged_paths(self, paths: Sequence[str] = ()) -> tuple[str, ...]:
        output = self._run("ls-files", "-z", "--", *paths).stdout
        return tuple(os.fsdecode(path) for path in output.split(b"\0") if path)

    def read_worktree_paths(self) -> tuple[str, ...]:
        output = self._run("ls-files", "-co", "--exclude-standard", "-z").stdout
        return tuple(os.fsdecode(path) for path in output.split(b"\0") if path)

    @contextmanager
    def open_worktree(self, revision: str | None = "HEAD") -> Generator["Repository"]:
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
                repository = type(self).init(location, str(self.executable))
                repository.stage((".",))
                yield repository
                return
            # A killed process cannot clean up after itself, so drop
            # the entries it left behind. Git only reclaims an entry
            # once its directory is gone from disk.
            self._run("worktree", "prune", check=False)
            self._run("worktree", "add", "--detach", str(location), revision)
            try:
                yield type(self)(location, str(self.executable))
            finally:
                removal = self._run("worktree", "remove", "--force", str(location), check=False)
                if removal.returncode:
                    logger.warning("worktree_removal_failed location=%s", location)

    def apply_patch(
        self,
        patch: bytes,
        *,
        check: bool = False,
        index: bool = False,
        directory: str | None = None,
        reverse: bool = False,
        zero_context: bool = False,
    ) -> None:
        # Recount hunk line counts from the patch body: models
        # routinely miscount them while the body itself is correct.
        arguments = ["apply", "--recount"]
        if zero_context:
            # Lift the two pins Git puts on a hunk with too little
            # context to be placed by: one holds a hunk without
            # trailing context against the end of its file, the other
            # holds a hunk whose header names line 1 against the start.
            # What places a hunk without them is the lines it quotes,
            # which still have to be in the file, at the occurrence
            # nearest the line its own header names.
            arguments.append("--unidiff-zero")
        if index:
            arguments.append("--index")
        if directory is not None:
            arguments.append(f"--directory={directory}")
        if reverse:
            arguments.append("--reverse")
        if check:
            arguments.append("--check")
        self._run(*arguments, stdin=patch)

    def stage(self, paths: Sequence[str], *, intent_to_add: bool = False, force: bool = False) -> None:
        arguments = ["add"]
        if intent_to_add:
            arguments.append("--intent-to-add")
        if force:
            arguments.append("--force")
        self._run(*arguments, "--", *paths)

    def unstage(self, paths: Sequence[str]) -> None:
        self._run("reset", "--quiet", "--", *paths)

    def restore(self, revision: str, paths: Sequence[str]) -> None:
        self._run("checkout", revision, "--", *paths)

    def commit(self, message: str, trailers: Sequence[str] = (), *, paths: Sequence[str] = ()) -> str:
        body = f"{message}\n\n{'\n'.join(trailers)}\n" if trailers else f"{message}\n"
        # Named paths are read from the worktree and written to the
        # index alone, so a commit of them carries nothing else and
        # disturbs nothing else that is staged or modified.
        arguments = ["commit", "--file=-"]
        if paths:
            arguments.extend(["--", *paths])
        self._run(*arguments, stdin=body.encode())
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
