#!/usr/bin/env -S uv run --script

import os
import re
import shutil
import subprocess
import tomllib
from argparse import ArgumentParser
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import check

# The gate runs `compileall` and writes these files under `src` on each run. uv_build excludes them from the wheel
# and sdist, so a release does not carry them.
CACHE_PATHSPEC = ":(exclude,glob)**/__pycache__/**"
# Git returns 0 and a repository name for a directory in a repository. It returns 128 for a directory it cannot
# read or that has no repository. These cases have the same result here. Any other result means Git did not check
# the directory. An empty result from that failure can make `check_remote` accept a remote, including this repository.
DIRECTORY_ANSWERS = frozenset({0, 128})
# `--only` writes only the named paths, but Git gives the pre-commit hook a temporary index in GIT_INDEX_FILE. A
# formatter hook can stage changes in that index, including paths that the gate did not read. `os.devnull` has no hook.
COMMIT_COMMAND = ("-c", f"core.hooksPath={os.devnull}", "commit", "--only", "--message", "chore: version")
NUMBER_PATTERN = re.compile(r"\d+\.\d+\.\d+")
# These are the two Git modes for a file with the same bytes as its blob. Any other mode for a carried path, such as
# a link or gitlink, points to bytes that the build reads from another location.
REGULAR_MODES = frozenset({"100644", "100755"})
# This command identifies a directory repository in a comparable form. The path is absolute because Git writes `.git`
# for its current repository. The common directory identifies a linked-worktree repository because its directory is
# private but its refs are shared.
REPOSITORY_COMMAND = ("rev-parse", "--path-format=absolute", "--git-common-dir")
# publish.yml releases the commit that a `v` tag points to. Tags record released versions. pyproject.toml states
# only the next version.
TAG_PREFIX = "v"
# `uv version` writes the first two paths. check.py owns the other paths.
BUMPED_PATHS = ("pyproject.toml", "uv.lock", *(path.as_posix() for path in check.VERSION_COPIES))


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("version")
    arguments = parser.parse_args()
    version = arguments.version
    root = Path(__file__).parent.parent
    uv = shutil.which("uv")
    git = shutil.which("git")
    if not uv or not git:
        raise RuntimeError("uv and git must both be installed")
    remote, ref = read_upstream(git, root)
    check_remote(git, root, remote)
    check_number(git, root, remote, version)
    check_upstream(git, root, remote, ref)
    check_changes(git, root, frozenset())
    head = _read_git(git, root, "rev-parse", "HEAD")
    pushed: str | None = None
    try:
        bump_version(uv, root, version)
        # publish.yml requires this exact gate run for a release. Stop the release here if it cannot pass the gate,
        # before it creates a tag.
        check.check_project(root, contracts=True)
        # The gate allows `ruff --fix` to rewrite files. A commit with only the version would push a tree the gate
        # did not check.
        check_changes(git, root, frozenset(BUMPED_PATHS))
        pushed = commit_bump(git, root)
        # The refspec selects the branch and does not use `push.default`. Do not use `--follow-tags`: publish.yml
        # releases each commit that a `v` tag points to, and no tag here passed the gate in this run.
        subprocess.run([git, "push", "--no-follow-tags", remote, f"HEAD:{ref}"], cwd=root, check=True)
    except BaseException:
        # This script creates the version bump and its commit. The remote can accept a commit while the gate runs. If
        # the release fails, remove both changes. This prevents a `chore: version` commit for a release that did not
        # go out.
        #
        # Catch all exits, not only check failures. The gate parses all files in `src` and `tests` before it runs
        # commands and can then run for minutes. Ctrl-C must end the run like a failed check. Otherwise, the next run
        # rejects the version in pyproject.toml and the changes in the tree.
        #
        # Use the remote, not the push result, to determine if the release failed. Git can fail after it updates the
        # remote ref: `post-receive` or `receive-pack` can stop, or the connection can close after the ref update. In
        # these cases, the push returns nonzero although it creates the release. Removing the bump would leave a remote
        # release with no local commit and make later pushes fail. If the remote does not answer, it does not show that
        # the release did not go out. Do not undo changes in this case. Leave the tree for a person to inspect.
        if pushed is None or _read_upstream_commit(git, root, remote, ref) != pushed:
            subprocess.run([git, "reset", "--soft", head], cwd=root, check=True)
            subprocess.run([git, "restore", "--staged", "--worktree", "--", *BUMPED_PATHS], cwd=root, check=True)
        raise


def check_number(git: str, root: Path, remote: str, version: str) -> None:
    number = _read_number(version)
    current = check.read_version(root)
    if number <= _read_number(current):
        raise RuntimeError(f"{version} does not come after {current}, the version pyproject.toml holds")
    # A branch can have a pyproject.toml version older than a tag. Without this check, it can release a version that
    # was already released. Its tag is free only because the earlier release uses a different tag.
    newest = max(_read_releases(git, root, remote), default=())
    if number <= newest:
        released = ".".join(str(part) for part in newest)
        raise RuntimeError(f"{version} does not come after {released}, the newest release a tag holds")


def read_upstream(git: str, root: Path) -> tuple[str, str]:
    branch = _read_git(git, root, "branch", "--show-current")
    if not branch:
        raise RuntimeError("HEAD is detached, so there is no branch to push")
    upstream = _read_git(
        git, root, "for-each-ref", "--format=%(upstream:remotename) %(upstream:remoteref)", f"refs/heads/{branch}"
    )
    if not upstream:
        raise RuntimeError(f"{branch} tracks nothing, so a push has nowhere to go")
    remote, ref = upstream.split()
    return remote, ref


def check_remote(git: str, root: Path, remote: str) -> None:
    # Git uses `.` as the remote for a branch that tracks a local branch. A remote with any name can also identify
    # this repository. Each guard reads the remote and the push writes to it. If the remote is this repository, the
    # guards read the tree that they check and the release returns to its start. Nothing then rejects the release.
    here = _read_git(git, root, *REPOSITORY_COMMAND)
    for url in _read_urls(git, root, remote):
        if _read_repository(git, root, url) == here:
            raise RuntimeError(f"{remote} is {url}, this repository, so a release would go nowhere")


def check_upstream(git: str, root: Path, remote: str, ref: str) -> None:
    upstream = _read_upstream_commit(git, root, remote, ref)
    if not upstream:
        return
    # If this repository does not contain a commit, this branch cannot contain it. Read the `merge-base` exit code.
    # Do not show its fatal message for an unknown commit name.
    reached = subprocess.run(
        [git, "merge-base", "--is-ancestor", upstream, "HEAD"], cwd=root, check=False, stderr=subprocess.DEVNULL
    )
    if reached.returncode:
        raise RuntimeError(f"{ref} on {remote} is at {upstream}, which this branch does not hold, so a push is refused")


def check_changes(git: str, root: Path, allowed: frozenset[str]) -> None:
    # `uv build` copies carried paths from the filesystem. It can ship an uncommitted file, regardless of .gitignore,
    # or a link or gitlink that points to bytes in another location. The gate does not read a change in the index but
    # not in the worktree. `bump_version` can overwrite that change on a bumped path. Git compares neither side of an
    # assume-unchanged or skip-worktree entry.
    carried = _read_carried(root)
    changed = {
        *_read_git(git, root, "diff", "--name-only", "HEAD").splitlines(),
        *_read_git(git, root, "diff", "--name-only", "--cached", "HEAD").splitlines(),
        *_read_git(git, root, "ls-files", "--others", "--exclude-standard").splitlines(),
        *_read_git(git, root, "ls-files", "--others", "--", *carried, CACHE_PATHSPEC).splitlines(),
        *(line[2:] for line in _read_git(git, root, "ls-files", "-v").splitlines() if not line.startswith("H ")),
        *_find_unheld(git, root, carried),
    }
    if unexpected := sorted(changed - allowed):
        raise RuntimeError("A release carries what a commit holds and nothing else:\n" + "\n".join(unexpected))


# Name the release from HEAD after the commit. Do not use the commit being created. The push writes HEAD to the
# remote, so the remote result can be compared with HEAD.
def commit_bump(git: str, root: Path) -> str:
    subprocess.run([git, *COMMIT_COMMAND, "--", *BUMPED_PATHS], cwd=root, check=True)
    return _read_git(git, root, "rev-parse", "HEAD")


def bump_version(uv: str, root: Path, version: str) -> None:
    current = check.read_version(root)
    subprocess.run([uv, "version", version, "--no-sync"], cwd=root, check=True)
    for path, spelling in check.VERSION_COPIES.items():
        copy = root / path
        text = copy.read_text(encoding="utf-8")
        bumped = text.replace(spelling.format(version=current), spelling.format(version=version))
        copy.write_text(bumped, encoding="utf-8")


def _find_unheld(git: str, root: Path, carried: tuple[str, ...]) -> Iterator[str]:
    # `-z` and this custom format keep each path name unchanged. Git default quoting can produce a path name that no
    # check can use.
    records = _read_git(
        git, root, "ls-files", "-z", "--format=%(objectmode) %(objectname) %(path)", "--", *carried
    ).split("\0")
    entries = [record.split(" ", 2) for record in records if record]
    yield from (path for mode, _, path in entries if mode not in REGULAR_MODES)
    # `--no-filters` hashes the disk bytes that the build copies. A `.gitattributes` filter, a working-tree encoding,
    # or another name for one inode can make Git report a clean worktree when its blob has other bytes. If Git records
    # a regular file that the filesystem does not contain, it is a deletion or type change. The diffs above already
    # report it, so do not hash it.
    regular = [entry for entry in entries if entry[0] in REGULAR_MODES and (root / entry[2]).is_file()]
    hashed = _read_git(git, root, "hash-object", "--no-filters", "--", *(entry[2] for entry in regular)).splitlines()
    yield from (entry[2] for entry, blob in zip(regular, hashed, strict=True) if entry[1] != blob)


def _read_carried(root: Path) -> tuple[str, ...]:
    # The wheel takes the module from `src`. The sdist also includes pyproject.toml, the readme, and the licences.
    # The wheel carries these files in its metadata and next to it.
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    licences = [
        path.relative_to(root).as_posix() for pattern in project["license-files"] for path in root.glob(pattern)
    ]
    return ("src", "pyproject.toml", project["readme"], *licences)


def _read_releases(git: str, root: Path, remote: str) -> list[tuple[int, ...]]:
    # A tag identifies a released version wherever it is. A local tag not yet pushed and a remote tag not yet fetched
    # both reserve a version. `ls-remote` writes `<oid>\trefs/tags/<tag>`, plus a second line for the commit that an
    # annotated tag peels to.
    tags = (
        *_read_git(git, root, "tag", "--list", f"{TAG_PREFIX}*").splitlines(),
        *_read_git(git, root, "ls-remote", "--tags", remote, f"{TAG_PREFIX}*").splitlines(),
    )
    numbers = {line.rpartition("/")[2].removesuffix("^{}").removeprefix(TAG_PREFIX) for line in tags}
    return [_read_number(number) for number in numbers if NUMBER_PATTERN.fullmatch(number)]


# A release went out when the remote holds its ref. `ls-remote` writes
# `<oid>\t<ref>` for an existing ref and writes nothing for a missing
# ref.
def _read_upstream_commit(git: str, root: Path, remote: str, ref: str) -> str:
    line = _read_git(git, root, "ls-remote", remote, ref)
    return line.split()[0] if line else ""


def _read_urls(git: str, root: Path, remote: str) -> Iterator[str]:
    # `--get-url` expands `insteadOf`. It returns a name with no configured remote unchanged, which is how `.` reaches
    # this code. A `pushurl` can send the push to a location that guards reading `url` do not check.
    yield _read_git(git, root, "ls-remote", "--get-url", remote)
    if remote in _read_git(git, root, "remote").splitlines():
        yield _read_git(git, root, "remote", "get-url", "--push", remote)


def _read_repository(git: str, root: Path, url: str) -> str:
    # Read a URL that Git reads as a directory as a directory here. Resolve a relative URL from the same root that Git
    # uses. `--git-common-dir` identifies the shared repository of a linked worktree, not the worktree. A URL that
    # does not name a local directory identifies a repository outside this run.
    split = urlsplit(url)
    read = subprocess.run(
        [git, "-C", root / (split.path if split.scheme == "file" else url), *REPOSITORY_COMMAND],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        encoding="utf-8",
    )
    if read.returncode not in DIRECTORY_ANSWERS:
        raise RuntimeError(f"Git ended at {read.returncode} over {url}, so whose repository it is went unanswered")
    return read.stdout.strip()


def _read_number(version: str) -> tuple[int, ...]:
    if not NUMBER_PATTERN.fullmatch(version):
        raise RuntimeError(f"{version} is not `major.minor.patch`, the way releases here are numbered")
    return tuple(int(part) for part in version.split("."))


def _read_git(git: str, root: Path, *arguments: str) -> str:
    # Git writes rejected operations to stderr. Read only its result and leave its error messages on the terminal.
    return subprocess.run(
        [git, *arguments], cwd=root, check=True, stdout=subprocess.PIPE, encoding="utf-8"
    ).stdout.strip()


if __name__ == "__main__":
    main()
