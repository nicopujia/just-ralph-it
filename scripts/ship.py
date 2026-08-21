#!/usr/bin/env -S uv run --script

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
# and sdist, so a release does not carry them. pyproject.toml names the other excluded paths in `source-exclude`.
CACHE_GLOB = "**/__pycache__/**"
# Git returns 0 and a repository name for a directory in a repository. It returns 128 for a directory it cannot
# read or that has no repository. These cases have the same result here. Any other result means Git did not check
# the directory. An empty result from that failure can make `check_remote` accept a remote, including this repository.
DIRECTORY_ANSWERS = frozenset({0, 128})
# The gate runs `ruff --fix` and can rewrite any file it reads. `--all` writes those rewrites with the version, so the
# release commit holds the tree the gate passed.
COMMIT_COMMAND = ("commit", "--all", "--message", "chore: version")
# A release carries the commit, and not the tree. Work in progress must leave the tree for the run and come
# back after it. `--include-untracked` takes new files too. Ignored files stay, because `source-exclude` and
# .gitignore agree on them.
STASH_COMMAND = ("stash", "push", "--include-untracked", "--message", "ship")
# A release moves the branch, the index, the tree, and the stash of one repository. Two releases at once each undo
# what the other wrote. Git leaves a name it does not know in the repository directory, so a release holds this one.
LOCK_NAME = "ship.lock"
NUMBER_PATTERN = re.compile(r"\d+\.\d+\.\d+")
# These are the two Git modes for a file with the same bytes as its blob. Any other mode for a carried path, such as
# a link or gitlink, points to bytes that the build reads from another location.
REGULAR_MODES = frozenset({"100644", "100755"})
# This command identifies a directory repository in a comparable form. The path is absolute because Git writes `.git`
# for its current repository. The common directory identifies a linked-worktree repository, because Git keeps the
# directory of such a worktree private and shares its refs.
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
    lock = hold_repository(git, root)
    try:
        release_version(uv, git, root, version, remote, ref)
    finally:
        # This run took the hold. No other run holds it now, so this run removes it.
        lock.unlink()


def release_version(uv: str, git: str, root: Path, version: str, remote: str, ref: str) -> None:
    stash = stash_changes(git, root)
    try:
        push_release(uv, git, root, version, remote, ref)
    finally:
        # The tree returns to the person who ran the release, whether it went out, stopped, or failed.
        restore_changes(git, root, stash)


def push_release(uv: str, git: str, root: Path, version: str, remote: str, ref: str) -> None:
    # The stash leaves the tree with what the commit holds. What the tree still holds is what a stash cannot
    # take, such as an entry Git compares on neither side. `uv build` can ship such an entry, but `git push`
    # cannot.
    check_changes(git, root)
    head = _read_git(git, root, "rev-parse", "HEAD")
    tag = f"{TAG_PREFIX}{version}"
    pushed: str | None = None
    try:
        # publish.yml runs this same gate over the commit the tag points to, and it checks the released tree
        # there. Run the gate here first, because a tree that cannot pass must stop before this run writes a
        # version it must then undo.
        check.check_project(root, contracts=True)
        bump_version(uv, root, version)
        pushed = commit_release(git, root, tag)
        # The commit holds every change the gate and the bump wrote. `uv build` can ship what the tree still
        # holds, but `git push` cannot.
        check_changes(git, root)
        # The refspecs select the branch and this one tag, and do not use `push.default`. Do not use
        # `--follow-tags`, because it would also push every other annotated tag, and no other tag passed the gate
        # in this run. `--atomic` writes both refs or neither. The remote never holds a release tag over a
        # commit the branch does not reach.
        subprocess.run(
            [git, "push", "--atomic", "--no-follow-tags", remote, f"HEAD:{ref}", f"refs/tags/{tag}"],
            cwd=root,
            check=True,
        )
    except BaseException:
        # This script creates the version bump and its commit. The remote can accept a commit while the gate
        # runs. If the release fails, remove both changes. A `chore: version` commit must not stay for a release
        # that did not go out.
        #
        # Catch all exits, not only check failures. The gate parses all files in `src` and `tests` before it runs
        # commands and can then run for minutes. Ctrl-C must end the run like a failed check. Otherwise, the next run
        # rejects the version in pyproject.toml and the changes in the tree.
        #
        # Read the tag on the remote, and not the push result, to know whether the release failed. The tag is
        # what publish.yml releases, and `--atomic` writes it with the branch. Git can fail after it updates both
        # refs. `post-receive` or `receive-pack` can stop, or the connection can close after Git updates the
        # refs. In these cases, the push returns nonzero although it creates the release. A run that removes the
        # bump would then leave a remote release with no local commit, and later pushes would fail. A run that
        # creates no commit creates no tag either, so it does not ask a remote that a failure here can make
        # unreachable.
        if pushed is None or not _read_upstream_commit(git, root, remote, f"refs/tags/{tag}"):
            # Move the branch back only from where this run left it. The hold stops another release, but a
            # person can still commit while the gate runs for minutes. This run must not remove that commit.
            if _read_git(git, root, "rev-parse", "HEAD") == (pushed or head):
                # `--mixed` leaves the index empty, as the stash left it. The rewrites the gate wrote stay in
                # the tree for the next release to carry. `restore_changes` can then write the stash over a tree
                # it can read.
                subprocess.run([git, "reset", "--mixed", head], cwd=root, check=True)
            # `--list` writes the name a tag holds and writes nothing for a free name. A version whose tag
            # another release holds does not reach this line, because `check_number` rejects it before the bump.
            if _read_git(git, root, "tag", "--list", tag):
                subprocess.run([git, "tag", "--delete", tag], cwd=root, check=True)
            subprocess.run([git, "restore", "--staged", "--worktree", "--", *BUMPED_PATHS], cwd=root, check=True)
        raise


def check_number(git: str, root: Path, remote: str, version: str) -> None:
    number = _read_number(version)
    current = check.read_version(root)
    if number <= _read_number(current):
        raise RuntimeError(f"{version} does not come after {current}, the version pyproject.toml holds")
    # A branch can have a pyproject.toml version older than a tag. Without this check, the script can release a
    # version that went out before. Its tag is free only because the earlier release uses a different tag.
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


def check_changes(git: str, root: Path) -> None:
    # `uv build` copies carried paths from the filesystem. It can ship an uncommitted file, regardless of .gitignore,
    # or a link or gitlink that points to bytes in another location. The gate does not read a change in the index but
    # not in the worktree. Git compares neither side of an assume-unchanged or skip-worktree entry.
    carried = _read_carried(root)
    changed = {
        *_read_git(git, root, "diff", "--name-only", "HEAD").splitlines(),
        *_read_git(git, root, "diff", "--name-only", "--cached", "HEAD").splitlines(),
        *_read_git(git, root, "ls-files", "--others", "--exclude-standard").splitlines(),
        *_read_git(git, root, "ls-files", "--others", "--", *carried).splitlines(),
        *(line[2:] for line in _read_git(git, root, "ls-files", "-v").splitlines() if not line.startswith("H ")),
        *_find_unheld(git, root, carried),
    }
    if unexpected := sorted(changed):
        raise RuntimeError("A release carries what a commit holds and nothing else:\n" + "\n".join(unexpected))


# Name the release from HEAD after the commit. Do not use the commit that this run creates. The push writes HEAD
# to the remote, so this run can compare the remote result with HEAD.
def commit_release(git: str, root: Path, tag: str) -> str:
    subprocess.run([git, *COMMIT_COMMAND], cwd=root, check=True)
    # publish.yml releases the commit this tag points to and reads the version from the tag name. This run annotates
    # the tag, as the release before it did, so the tag records who released and when.
    subprocess.run([git, "tag", "--annotate", "--message", tag, tag], cwd=root, check=True)
    return _read_git(git, root, "rev-parse", "HEAD")


def bump_version(uv: str, root: Path, version: str) -> None:
    current = check.read_version(root)
    subprocess.run([uv, "version", version, "--no-sync"], cwd=root, check=True)
    for path, spelling in check.VERSION_COPIES.items():
        copy = root / path
        text = copy.read_text(encoding="utf-8")
        bumped = text.replace(spelling.format(version=current), spelling.format(version=version))
        copy.write_text(bumped, encoding="utf-8")
    # `replace` writes the same text when a copy spells the version another way, and the release would then
    # carry two versions. The gate reads the version before this line. This call makes the reading that the gate
    # cannot make.
    check.check_version(root)


def hold_repository(git: str, root: Path) -> Path:
    # `--git-common-dir` names the one directory that every linked worktree of this repository shares. A
    # release from any worktree meets the same name. A release that ends without removing the name leaves it for
    # a person to remove. That name stops a release over a tree that an earlier release left as it was.
    lock = Path(_read_git(git, root, *REPOSITORY_COMMAND)) / LOCK_NAME
    try:
        lock.touch(exist_ok=False)
    except FileExistsError:
        raise RuntimeError(f"{lock} says another release holds this repository, so this release stops") from None
    return lock


def stash_changes(git: str, root: Path) -> str:
    before = _read_stash(git, root)
    subprocess.run([git, *STASH_COMMAND], cwd=root, check=True)
    after = _read_stash(git, root)
    # `stash push` writes no entry when the tree holds nothing to set aside. Name the entry this run created, so a
    # run that set nothing aside restores nothing.
    return after if after != before else ""


def restore_changes(git: str, root: Path, stash: str) -> None:
    if not stash:
        return
    # `pop` takes the newest entry. Another entry on top of this one belongs to somebody else. Leave every entry for
    # that person instead of returning their work to this tree.
    if _read_stash(git, root) != stash:
        raise RuntimeError(f"The newest stash entry is no longer {stash}, so the changes it holds stay where they are")
    subprocess.run([git, "stash", "pop"], cwd=root, check=True)


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
    # The wheel carries these files in its metadata and next to it. The exclude pathspecs drop the paths the build
    # leaves behind, so no guard reads a file that no release carries.
    settings = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = settings["project"]
    licences = [
        path.relative_to(root).as_posix() for pattern in project["license-files"] for path in root.glob(pattern)
    ]
    excluded = (CACHE_GLOB, *settings["tool"]["uv"]["build-backend"].get("source-exclude", ()))
    return ("src", "pyproject.toml", project["readme"], *licences, *(f":(exclude,glob){glob}" for glob in excluded))


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


# `--verify --quiet` writes the commit an existing ref points to and writes nothing for a repository with no stash.
def _read_stash(git: str, root: Path) -> str:
    read = subprocess.run(
        [git, "rev-parse", "--verify", "--quiet", "refs/stash"],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        encoding="utf-8",
    )
    return read.stdout.strip()


def _read_urls(git: str, root: Path, remote: str) -> Iterator[str]:
    # `--get-url` expands `insteadOf`. It returns a name with no configured remote unchanged, which is how `.` reaches
    # this code. A `pushurl` can send the push to a location that guards reading `url` do not check.
    yield _read_git(git, root, "ls-remote", "--get-url", remote)
    if remote in _read_git(git, root, "remote").splitlines():
        yield _read_git(git, root, "remote", "get-url", "--push", remote)


def _read_repository(git: str, root: Path, url: str) -> str:
    # Git reads some URLs as a directory. Read such a URL as a directory here too. Resolve a relative URL from
    # the same root that Git uses. `--git-common-dir` identifies the shared repository of a linked worktree, and
    # not the worktree. A URL that does not name a local directory identifies a repository outside this run.
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
