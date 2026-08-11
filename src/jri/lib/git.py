import logging
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Collection, Generator, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, Self

__all__ = [
    "ROOT_ANSWERS",
    "Error",
    "Locks",
    "NotInstalledError",
    "NotRepositoryError",
    "Repository",
    "Status",
    "find_root",
]

# `rev-parse` returns 0 if a worktree contains the path. It returns fatal code 128 if no worktree contains it.
# The path can be outside a repository, in a bare repository, or absent.
ROOT_ANSWERS = frozenset({0, 128})

logger = logging.getLogger(__name__)


def find_root(path: Path) -> Path | None:
    executable = shutil.which("git")
    if executable is None:
        return None
    result = subprocess.run(
        [executable, "-C", str(path), "rev-parse", "--show-toplevel"], check=False, capture_output=True
    )
    _check_root_answered(result, path)
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


# Git uses a `.lock` file to stop two commands from writing the same file. Git creates `<file>.lock` beside
# the file, writes it, then renames it over the file. If the command stops first, the lock remains. Later
# commands that need the file fail. `directories` lists repository-lock directories. `written` identifies files
# that repository commands write. This lets the code separate blocking locks from unrelated locks.
@dataclass(frozen=True)
class Locks:
    SUFFIX: ClassVar[str] = ".lock"
    # Do not scan locks in these directories. `gc` and `maintenance` lock the object store, but this code does not
    # wait for them. Each worktree has its own directory. Its `Repository` handles its locks.
    UNGUARDED: ClassVar[frozenset[str]] = frozenset({"objects", "worktrees"})

    directories: tuple[Path, ...]
    written: tuple[Path, ...] = ()

    @property
    def standing(self) -> frozenset[Path]:
        found: set[Path] = set()
        for root in set(self.directories):
            for directory, names, files in os.walk(root):
                names[:] = [name for name in names if name not in self.UNGUARDED]
                found.update(Path(directory) / name for name in files if name.endswith(self.SUFFIX))
        return frozenset(found)

    @property
    def blocking(self) -> tuple[Path, ...]:
        locks = (Path(f"{path}{self.SUFFIX}") for path in self.written)
        return tuple(lock for lock in locks if lock.exists())

    # Remove only blocking locks that appear after the command starts. An earlier lock, or a lock for a file that
    # these commands do not write, can belong to another active command. Do not remove that lock.
    def release(self, standing: Collection[Path]) -> None:
        self._remove(frozenset(self.blocking) - frozenset(standing))

    # Remove all blocking locks, without considering creation time. Call this only when no command in this
    # repository can run. This code cannot separate a stale lock from a lock that an active command holds.
    def clear(self) -> None:
        self._remove(self.blocking)

    @staticmethod
    def _remove(paths: Iterable[Path]) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # Windows cannot unlink a file that another process has opened. This file is an active lock.
                logger.exception("git_lock_release_failed path=%s", path)
            else:
                logger.info("git_lock_released path=%s", path)


class Repository:
    # Git question commands return 0 for yes and 1 for no. They return no other code. `--quiet` gives a failure
    # here instead of fatal code 128.
    ANSWERS: ClassVar[frozenset[int]] = frozenset({0, 1})
    # Git handles these signals. `sigchain_push_common` sets one handler to remove all command locks before the
    # default signal action ends the process. A listed signal then leaves no command locks. Use the available values
    # because Windows defines only two of the five signal names.
    HANDLED_SIGNALS: ClassVar[frozenset[int]] = frozenset(
        member for member in signal.Signals if member.name in {"SIGHUP", "SIGINT", "SIGPIPE", "SIGQUIT", "SIGTERM"}
    )
    # These commands cause Git to lock the index before its first write. Git keeps the lock until its last write.
    # When Git gets the lock, only that Git owns it. Git creates it with `O_EXCL`. No other Git removes it. This
    # code cannot identify a lock that exists before then. See `_held_the_index`. Read commands and `worktree` never
    # take this lock. `apply` and `commit` take it for part of their operations, as `_held_the_index` defines.
    INDEX_HOLDERS: ClassVar[frozenset[str]] = frozenset({"add", "checkout", "reset"})
    # Git records symbolic links with this mode in the index and in each tree that it writes.
    LINK_MODE: ClassVar[str] = "120000"
    # This prefix identifies a HEAD that refers to a ref, not to a direct commit.
    SYMBOLIC_HEAD: ClassVar[str] = "ref: "
    # Git uses this temporary index while a named-path commit builds the project index. Its name includes the Git
    # process ID. Thus, two Git processes do not build the same index.
    TEMPORARY_INDEX: ClassVar[str] = "next-index-"

    def __init__(self, path: Path | str, executable: str = "git") -> None:
        resolved_executable = shutil.which(executable)
        if resolved_executable is None:
            raise NotInstalledError(f"Git executable not found: {executable}")
        self.executable = Path(resolved_executable)
        candidate = Path(path).resolve()
        result = subprocess.run(
            [
                str(self.executable),
                "-C",
                str(candidate),
                "rev-parse",
                "--show-toplevel",
                "--absolute-git-dir",
                "--git-common-dir",
            ],
            check=False,
            capture_output=True,
        )
        _check_root_answered(result, candidate)
        if result.returncode:
            raise NotRepositoryError(os.fsdecode(result.stderr).strip() or f"Not a Git worktree: {candidate}")
        # Git returns these three values in request order. The repository directory in a worktree is not always
        # `.git`. A link, `GIT_DIR`, or a linked worktree can put it elsewhere. Each linked worktree has its own
        # directory for private data, such as its index and HEAD. Each linked worktree shares common data with the
        # first worktree. Git returns the common directory relative to the command directory.
        top_level, git_directory, common_directory = os.fsdecode(result.stdout).splitlines()
        self.path = Path(top_level).resolve()
        self._git_directory = Path(git_directory).resolve()
        self._common_directory = (candidate / common_directory).resolve()

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
        # Other commands use `_run`. It records existing locks and removes only locks that its command left. `init`
        # runs before a repository exists. Each lock that it removes was left by a stopped run. `find_root` rejects
        # this path as a repository. Locks protect commands in one repository, so no command in this repository runs
        # here. `init` writes config and then HEAD with separate locks. A stop between the writes leaves a lock that
        # stops later `init` calls. No `Repository` can be created for that `.git` directory to report the lock.
        directory = candidate / ".git"
        Locks((directory,), (directory / "config", directory / "HEAD")).clear()
        result = subprocess.run(
            [resolved_executable, "-C", str(candidate), "init", "--quiet"], check=False, capture_output=True
        )
        if result.returncode:
            raise NotRepositoryError(os.fsdecode(result.stderr).strip() or f"Cannot initialize Git: {candidate}")
        return cls(candidate, executable)

    # These locks can stop commands here: the index lock and the two ref locks that a commit moves. An unborn
    # branch counts because HEAD names the first commit target before that commit exists. Read the HEAD target from
    # its file, not from Git. This code does this before and after each command, and a Git query runs another command.
    # A commit also locks `AUTO_MERGE` and `packed-refs`, but exclude them. A stale lock for either prints an error
    # while the commit exits 0. Thus, neither lock stops a command.
    @property
    def locks(self) -> Locks:
        written = [self._git_directory / "index", self._git_directory / "HEAD"]
        branch = self._read_branch()
        if branch is not None:
            written.append(self._common_directory / branch)
        return Locks((self._git_directory, self._common_directory), tuple(written))

    def has_commit(self, revision: str = "HEAD") -> bool:
        return not self._ask("rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}").returncode

    def has_conflicts(self) -> bool:
        return bool(self._run("ls-files", "--unmerged", "-z").stdout)

    def is_on_branch(self) -> bool:
        # Include an unborn branch. HEAD names the first commit target before that commit exists.
        return not self._ask("symbolic-ref", "--quiet", "HEAD").returncode

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

    # `--ignore-missing` keeps the Git exit status as the answer. An unborn HEAD has no commit, but Git exits 0.
    # Thus, empty output means Git searched and found no match. Without this option, this case is fatal. To separate
    # it from another fatal error, this code needs another query. A stopped Git can give no answer to that query.
    def find_commit(self, text: str, revision: str = "HEAD") -> str | None:
        arguments = (
            "log",
            "--max-count=1",
            "--format=%H",
            "--fixed-strings",
            f"--grep={text}",
            "--ignore-missing",
            revision,
        )
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

    # Return index paths for these pathspecs. If `linked` is true, return only paths with an index mode for a
    # symbolic link. A symbolic link is an index mode, not a worktree shape. On a system that cannot create links,
    # such as Windows without the required privilege, checkout writes a plain file with target text. `git add`
    # restores the symbolic-link mode. The mode remains in worktrees that cannot show a link. Query the index, not
    # a commit, because the next commit uses the index.
    def read_staged_paths(self, paths: Sequence[str] = (), *, linked: bool = False) -> tuple[str, ...]:
        # The format is `<mode> SP <object> SP <stage> TAB <path>`. Only `-z` keeps path bytes, not a quoted
        # representation.
        output = self._run("ls-files", "--stage", "-z", "--", *paths).stdout
        records = (os.fsdecode(record).split(" ", 2) for record in output.split(b"\0") if record)
        entries = ((mode, rest.partition("\t")[2]) for mode, _, rest in records)
        return tuple(path for mode, path in entries if not linked or mode == self.LINK_MODE)

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
            # A stopped process cannot remove its worktree entries. Remove them now. Git reclaims an entry only after
            # its directory is removed from disk.
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
        arguments = ["apply"]
        if zero_context:
            # `--unidiff-zero` removes two Git location limits for a hunk with too little context. One limit requires
            # a hunk with no trailing context to be at the end of its file. The other requires a hunk with line 1 in
            # its header to be at the start of its file. Git then locates the hunk from its quoted lines. These lines
            # must still occur nearest the line in the hunk header.
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
        # Git reads named paths from the worktree and writes only the index. A commit for these paths includes no
        # other staged or modified data, and it changes none.
        arguments = ["commit", "--file=-"]
        if paths:
            arguments.extend(["--", *paths])
        self._run(*arguments, stdin=body.encode())
        return self.read_head()

    def _run(
        self, *arguments: str, stdin: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        # A Git read can lock the index while it refreshes the index. If the read stops, the lock remains although the
        # repository did not change. `--no-optional-locks` prevents only this write. A command that needs the index
        # lock, such as a commit, still takes it.
        command = [
            str(self.executable),
            "--no-optional-locks",
            # A commit can start Git `maintenance` after it exits. It locks the object store and repacks caller data.
            # This work was not requested. It lasts after the command ends, so the command cannot report its lock.
            "-c",
            "maintenance.auto=false",
            "-C",
            str(self.path),
            *arguments,
        ]
        locks = self.locks
        standing = locks.standing
        # Start the process, not only run it, to get its process ID. The temporary index file below includes its
        # creator process ID. The creator is this process's child.
        with subprocess.Popen(
            command,
            stdin=subprocess.PIPE if stdin is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            output, errors = process.communicate(stdin)
        result = subprocess.CompletedProcess(command, process.returncode, output, errors)
        # Git removes locks when it exits normally. It also removes locks before the default signal action in
        # `HANDLED_SIGNALS` ends the process. A negative exit code can indicate a remaining lock only for a signal
        # that Git does not handle. `communicate` has reaped the process, so a lock that it left is not active. Remove
        # only files that this Git command could own and that were not locked before it started. A named-path commit
        # has a temporary index whose name includes the child Git process ID. That lock belongs to this Git. Include
        # the project index only if `_held_the_index` says that the command held it. Do not remove HEAD or branch
        # locks. Only a commit updates them in its final reference transaction. It holds neither lock before then, so
        # both are free while commit hooks run. A stopped process does not show if it stopped before or after this
        # transaction. All other commands leave these files free, so their locks belong to another command that can
        # still rename its lock over the file. Leave that lock. A later command fails, but removal can damage that
        # commit. On Windows, exit codes do not report signals. Leave locks from stopped Git processes until Git
        # reports a lock error. The same safety rule applies.
        if result.returncode < 0 and -result.returncode not in self.HANDLED_SIGNALS:
            index, *_ = locks.written
            written = [self._git_directory / f"{self.TEMPORARY_INDEX}{process.pid}"]
            if self._held_the_index(arguments):
                written.append(index)
            replace(locks, written=tuple(written)).release(standing)
        if check and result.returncode:
            self._raise(result)
        return result

    # Return whether this Git command held the project index lock. It can hold it before its first index write,
    # through its last. Use command arguments, not the caller, because Git gets these arguments. `apply` holds the
    # lock when it uses the index. A named-path commit builds a temporary index. It copies it over the project index
    # at the end. A commit for staged content updates the project index in `prepare_index` before its first hook.
    # Later hooks and reference transactions run when the project index is unlocked. This does not cover time from
    # process start until Git requests the index lock. Git first loads and reads configuration. In a small repository,
    # this interval is most of each command. It is 80% of `add`, 88% of `reset`, and 89% of `checkout`. It is 69% of
    # a named-path commit. These values were measured with Git 2.54 on Linux. A stop in this time has the same exit
    # code and output as a stop after Git gets the lock. Another command can then take the lock. This code can read
    # the lock as this Git lock. It can remove it before another command renames it over the index. Unlike
    # `next-index-<pid>`, the index lock has no creator process ID. Its size, content, and exit state are the same on
    # either side. The only way to remove this uncertainty is to never remove index locks. That prevents recovery
    # after a process stops during staging. This cleanup provides that recovery, but creates a race if another Git
    # process takes the lock in this interval. This code accepts that trade-off. Keep this comment so changes
    # consider both effects.
    def _held_the_index(self, arguments: Sequence[str]) -> bool:
        if arguments[0] in self.INDEX_HOLDERS:
            return True
        if arguments[0] == "apply":
            return "--index" in arguments
        return arguments[0] == "commit" and "--" in arguments

    # An exit that Git selects is an answer. An external stop is not an answer. A signal can bypass Git exit and
    # signal handlers. Examples include an out-of-memory kill, `pkill git`, or a killed project hook. A signal can
    # give no output and a nonzero exit code, like a negative answer. Treating it as no would assert unchecked state:
    # a repository with no commit, a HEAD on no branch, or a branch with no lock.
    def _ask(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        result = self._run(*arguments, check=False)
        if result.returncode not in self.ANSWERS:
            self._raise(result)
        return result

    # Return the ref that a symbolic HEAD names. The HEAD file contains the marker and ref name. A detached HEAD
    # contains an object ID and names no branch. An unreadable HEAD also names no branch. `is_on_branch` asks Git
    # for the state. `blocking` reports a lock that this method cannot identify. `release` does not remove that lock.
    def _read_branch(self) -> str | None:
        try:
            head = os.fsdecode((self._git_directory / "HEAD").read_bytes()).strip()
        except OSError:
            return None
        return head.removeprefix(self.SYMBOLIC_HEAD) if head.startswith(self.SYMBOLIC_HEAD) else None

    @staticmethod
    def _raise(result: subprocess.CompletedProcess[bytes]) -> None:
        message = os.fsdecode(result.stderr).strip() or os.fsdecode(result.stdout).strip()
        raise Error(message or "Git command failed.")


# This is the final check. If Git stops during this query, it gives no output and a nonzero exit code, like a
# path outside every worktree. Another Git query only moves the uncertainty because the cause can stop either
# process. Use the available kernel exit code and expected codes to separate the cases.
def _check_root_answered(result: subprocess.CompletedProcess[bytes], path: Path) -> None:
    if result.returncode in ROOT_ANSWERS:
        return
    raise Error(
        os.fsdecode(result.stderr).strip()
        or f"Git ended at {result.returncode} over {path}, so which worktree holds it went unanswered"
    )
