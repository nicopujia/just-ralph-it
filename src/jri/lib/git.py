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

# The endings `rev-parse` gives the question of which worktree holds a
# path: nought carrying the answer, and the 128 a fatal ends at, which
# is where every path no worktree holds arrives -- one outside every
# repository, one in a bare one, one that is not there at all.
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


# Git's guard against two commands writing one file at once, and the
# only shape it ever has: the file's own name with `.lock` after it,
# made where the file lives, written into and renamed over the file. So
# a command that dies in between leaves one standing, and every later
# command that wants that file refuses. `directories` are where a
# repository's own locks live and `written` the files its commands
# write, which is what tells a lock that stops one from a lock that
# does not.
@dataclass(frozen=True)
class Locks:
    SUFFIX: ClassVar[str] = ".lock"
    # Where a lock says nothing about a command of this repository's:
    # `gc` and `maintenance` lock the object store, which nothing run
    # here ever waits for, and every other worktree keeps its own
    # directory, which its own `Repository` answers for.
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

    # What stops these commands, stands now and did not stand then,
    # and nothing else: a lock already standing is whosever it already
    # was, and a lock over a file these commands never write is one
    # nothing here can name the holder of -- a second command's, for
    # all this can tell, whose own process is still going to use it.
    def release(self, standing: Collection[Path]) -> None:
        self._remove(frozenset(self.blocking) - frozenset(standing))

    # Every lock that stops these commands, whenever it was taken. Only
    # a caller holding a reason no command of this repository's can be
    # running may ask for it, since there is no `then` here to tell a
    # lock a dead command left from one a live command holds.
    def clear(self) -> None:
        self._remove(self.blocking)

    @staticmethod
    def _remove(paths: Iterable[Path]) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # Windows refuses to unlink a file another process
                # holds open, which is a lock that is being held.
                logger.exception("git_lock_release_failed path=%s", path)
            else:
                logger.info("git_lock_released path=%s", path)


class Repository:
    # The endings Git gives a question its own ending answers: nought
    # for yes, one for no, and nothing else -- `--quiet` is what puts a
    # refusal here rather than at the 128 a fatal ends with.
    ANSWERS: ClassVar[frozenset[int]] = frozenset({0, 1})
    # The signals Git is asked to stop at. `sigchain_push_common` gives
    # all of them one handler, which takes away every lock the command
    # holds and only then lets the default action end the process, so a
    # death by one of these is reported as a signal over a command that
    # left nothing of its own. Read off the numbers this platform has,
    # since Windows names two of the five.
    HANDLED_SIGNALS: ClassVar[frozenset[int]] = frozenset(
        member for member in signal.Signals if member.name in {"SIGHUP", "SIGINT", "SIGPIPE", "SIGQUIT", "SIGTERM"}
    )
    # The commands whose Git takes the index lock before it writes
    # anything and keeps it to its last write whatever else the line
    # asked of them, so that a lock over the index they leave behind is
    # one nothing else could have made: Git makes a lock file with
    # `O_EXCL`, and this Git had it. Every read and every `worktree`
    # takes it at no point at all. `apply` and `commit` hold it over
    # part of what they are asked for and over the rest not at all,
    # which is the reading `_held_the_index` makes of the line.
    INDEX_HOLDERS: ClassVar[frozenset[str]] = frozenset({"add", "checkout", "reset"})
    # The mode Git records a symbolic link under, in the index and in
    # every tree written from it.
    LINK_MODE: ClassVar[str] = "120000"
    # What HEAD holds in front of the ref it stands for, where it
    # stands for one rather than for a commit of its own.
    SYMBOLIC_HEAD: ClassVar[str] = "ref: "
    # Where a commit of named paths composes the index it goes on to
    # copy over the project's own, up to the number of the Git that
    # writes it: no two Gits ever compose the same one.
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
        # Git answers the three in the order they were asked for, and
        # the directory holding the repository is not `.git` under the
        # worktree wherever a link, a `GIT_DIR` or a second worktree
        # puts it somewhere else. A second worktree has a directory of
        # its own for what is only its -- its index, its HEAD -- and
        # shares the first one's for what every worktree has in common,
        # which Git answers for relative to where it was asked.
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
        # Every other command here runs inside `_run`, which knows what
        # stood before it and takes away only what it left. `init` runs
        # before there is a repository to snapshot, and the lock it has
        # to get past belongs to a run that is already gone. What says
        # so is the refusal above: a lock guards one command of a
        # repository's against another, and Git will not call this path
        # a repository, so no command of one is running here. `init`
        # writes the config and then HEAD, each under its own lock, and
        # a kill between the two leaves one standing that stops every
        # `init` after it -- over a `.git` no `Repository` can be built
        # on to report it.
        directory = candidate / ".git"
        Locks((directory,), (directory / "config", directory / "HEAD")).clear()
        result = subprocess.run(
            [resolved_executable, "-C", str(candidate), "init", "--quiet"], check=False, capture_output=True
        )
        if result.returncode:
            raise NotRepositoryError(os.fsdecode(result.stderr).strip() or f"Cannot initialize Git: {candidate}")
        return cls(candidate, executable)

    # The files every command run here writes, so the only locks that
    # ever stop one: the index, and the two refs a commit moves. An
    # unborn branch counts, since HEAD names the branch a first commit
    # would land on before that commit exists. Where HEAD points is
    # read off the file Git keeps it in rather than asked of Git, since
    # this is asked either side of every command run here and asking
    # would run one of its own.
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
        # An unborn branch counts: HEAD names it before any commit
        # exists, and that is where a first commit would land.
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

    # `--ignore-missing` is what leaves Git's own ending as the answer:
    # a revision naming nothing -- an unborn HEAD -- reaches no commit
    # and Git still ends at nought, so nought is Git having looked and
    # the empty answer is what it found. Without it that case is a
    # fatal, and telling that fatal from every other one means asking a
    # second question first, whose `no` a killed Git gives just as
    # readily as a Git that looked.
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

    # Every path the index holds under these pathspecs, or, where
    # `linked` asks for them, only the ones it holds as symbolic links.
    # A link is a mode the index records rather than a shape the
    # worktree has to have: where the platform makes none -- a Windows
    # without the privilege for one -- a checkout writes such an entry
    # out as a plain file holding the target's text, and `git add` over
    # that file records the link straight back, so the mode outlives
    # every worktree that cannot show it. The index answers rather than
    # a commit, because the index is what the next commit is written
    # from.
    def read_staged_paths(self, paths: Sequence[str] = (), *, linked: bool = False) -> tuple[str, ...]:
        # `<mode> SP <object> SP <stage> TAB <path>`, and only `-z`
        # leaves the path as the bytes it is rather than as a quoted
        # rendering of them.
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
        arguments = ["apply"]
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
        # A read of Git's takes the index lock to write the index it
        # refreshed on the way, and a read killed inside that leaves the
        # lock standing over a repository nothing was changing. This
        # drops the write and nothing else: a command that has to have
        # the lock, like a commit, still takes it.
        command = [
            str(self.executable),
            "--no-optional-locks",
            # A commit hands the repository to a `git maintenance` of
            # Git's own that outlives it, holds the object store behind
            # it and repacks everything the caller has. That is work
            # nothing here asked for, and a lock the command that ended
            # cannot answer for.
            "-c",
            "maintenance.auto=false",
            "-C",
            str(self.path),
            *arguments,
        ]
        locks = self.locks
        standing = locks.standing
        # Started rather than run whole, so the pid is in hand: the one
        # file below that says which process wrote it says so in its
        # name, and the process it names is this one's own child.
        with subprocess.Popen(
            command,
            stdin=subprocess.PIPE if stdin is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            output, errors = process.communicate(stdin)
        result = subprocess.CompletedProcess(command, process.returncode, output, errors)
        # Git takes its own locks away as a command of its ends, by an
        # exit handler for the ending it chose and by the one handler
        # `HANDLED_SIGNALS` names, which runs before the default action
        # the kernel then reports the death as. So a status below
        # nought is not on its own a command that left something: only
        # one naming a signal Git never handled is. Such a process is
        # reaped by the time this reads, which is what makes a lock it
        # left a lock nothing is holding. What that death answers for
        # is the files this command's own Git could have been holding,
        # and only the ones it did not find already locked. The index a
        # commit of named paths composes is its own Git's outright: the
        # lock over it carries the number of the Git that made it, and
        # that Git is the child this started. The project's index goes
        # with it only under a command that had the lock over it, and
        # HEAD and the branch only under a commit, the one command here
        # that moves them. Under anything else those files are free for
        # the whole span, so a lock over one is a second command's,
        # whose own process is still there to rename it over what it
        # guards, and naming it is all this may do -- what a lock left
        # standing costs the run that meets it is a refusal, and what
        # taking it away costs the commit holding it is that commit. On
        # Windows no signal reaches the exit code, so a lock a killed
        # Git left stands until Git's own refusal names it, which is
        # the side to be wrong on for the same reason.
        if result.returncode < 0 and -result.returncode not in self.HANDLED_SIGNALS:
            index, *refs = locks.written
            written = [self._git_directory / f"{self.TEMPORARY_INDEX}{process.pid}"]
            if self._held_the_index(arguments):
                written.append(index)
            if arguments[0] == "commit":
                written.extend(refs)
            replace(locks, written=tuple(written)).release(standing)
        if check and result.returncode:
            self._raise(result)
        return result

    # Whether this command's own Git held the lock over the project's
    # index from before its first write of it to its last. Read off the
    # line rather than off the caller that built it, since the line is
    # what Git answered to. An `apply` held it where it was asked for
    # the index. A commit held it where it names the paths it is of,
    # which it composes an index of its own from and copies over the
    # project's at the very end; a commit of everything already staged
    # writes the index it refreshed under that lock and renames it over
    # the file inside `prepare_index`, ahead of the first hook, so every
    # hook and every reference transaction after it stands where the
    # index is free and a second command has that whole span to take it
    # in.
    def _held_the_index(self, arguments: Sequence[str]) -> bool:
        if arguments[0] in self.INDEX_HOLDERS:
            return True
        if arguments[0] == "apply":
            return "--index" in arguments
        return arguments[0] == "commit" and "--" in arguments

    # An ending Git chose is an answer; an ending chosen for it is not.
    # A signal lands where neither Git's exit handler nor its signal
    # handlers reach -- an out-of-memory kill, a `pkill git`, a hook of
    # the project's whose Git is killed -- and leaves the same silence
    # and the same non-zero status a `no` leaves. Read as `no`, that
    # silence is a state nothing looked at: a repository holding no
    # commit, a HEAD on no branch, a branch whose lock nothing guards.
    def _ask(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        result = self._run(*arguments, check=False)
        if result.returncode not in self.ANSWERS:
            self._raise(result)
        return result

    # The branch HEAD stands for. Git keeps HEAD in one file, whose
    # whole content is the marker and the name of the ref it points
    # at; a detached HEAD holds an object id there instead and stands
    # for no branch. A HEAD nothing can read stands for none either,
    # and what a repository in that state is, `is_on_branch` asks Git:
    # a lock this cannot name is one `blocking` reports rather than one
    # a release takes away.
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


# Where the regress stops. A Git killed at this question leaves the
# same silence and the same not-nought a path outside every worktree
# leaves, and asking a second Git which of the two it was only moves
# the silence one process along: the causes are aimed at no single
# process, so the Git that would confirm dies as readily as the Git
# that was asked. What tells them apart without asking anything is the
# ending already in hand -- not Git's word but the kernel's report of
# how Git ended -- weighed against the endings this question has.
def _check_root_answered(result: subprocess.CompletedProcess[bytes], path: Path) -> None:
    if result.returncode in ROOT_ANSWERS:
        return
    raise Error(
        os.fsdecode(result.stderr).strip()
        or f"Git ended at {result.returncode} over {path}, so which worktree holds it went unanswered"
    )
