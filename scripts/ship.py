#!/usr/bin/env -S uv run --script

import os
import re
import shutil
import subprocess
import tomllib
from argparse import ArgumentParser
from collections.abc import Iterator
from pathlib import Path

import check

# The gate's own `compileall` writes these under `src` on every run
# and uv_build keeps them out of the wheel and the sdist alike, so
# they are the one thing there a release does not carry.
CACHE_PATHSPEC = ":(exclude,glob)**/__pycache__/**"
# `--only` writes the paths it names, but it writes them through a
# temporary index git hands the pre-commit hook in GIT_INDEX_FILE, and
# a formatter hook that stages what it rewrites has that in the commit
# -- any path, read by the gate or not. Nothing under `os.devnull` is
# a hook.
COMMIT_COMMAND = ("-c", f"core.hooksPath={os.devnull}", "commit", "--only", "--message", "chore: version")
NUMBER_PATTERN = re.compile(r"\d+\.\d+\.\d+")
# The two modes git gives a file whose bytes are the blob's own. A
# carried path held under any other -- a link, a gitlink -- names
# bytes the build reads from wherever the entry points instead.
REGULAR_MODES = frozenset({"100644", "100755"})
# publish.yml releases whatever a `v` tag points at, so the tags are
# the record of what went out and pyproject.toml only says what is next.
TAG_PREFIX = "v"
# `uv version` writes the first two; check.py owns the rest.
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
    check_number(git, root, remote, version)
    check_upstream(git, root, remote, ref)
    check_changes(git, root, frozenset())
    head = _read_git(git, root, "rev-parse", "HEAD")
    bump_version(uv, root, version)
    try:
        # publish.yml gates a release on this exact run, so a release
        # that cannot pass it is stopped here rather than on the tag.
        check.check_project(root, contracts=True)
        # The gate lets `ruff --fix` rewrite what it fixes, and a commit
        # of the version alone would push a tree nothing checked.
        check_changes(git, root, frozenset(BUMPED_PATHS))
        subprocess.run([git, *COMMIT_COMMAND, "--", *BUMPED_PATHS], cwd=root, check=True)
        # The refspec leaves `push.default` no say in which branches go,
        # and `--follow-tags` is refused because publish.yml releases
        # whatever a `v` tag points at and no tag here has passed the
        # gate this run just ran.
        subprocess.run([git, "push", "--no-follow-tags", remote, f"HEAD:{ref}"], cwd=root, check=True)
    except (RuntimeError, subprocess.CalledProcessError):
        # The bump and the commit holding it are this script's own, and
        # the remote can take a commit while the gate runs, so a release
        # turned down at the push takes both back out rather than
        # leaving a `chore: version` for a release that never went out.
        subprocess.run([git, "reset", "--soft", head], cwd=root, check=True)
        subprocess.run([git, "restore", "--staged", "--worktree", "--", *BUMPED_PATHS], cwd=root, check=True)
        raise


def check_number(git: str, root: Path, remote: str, version: str) -> None:
    number = _read_number(version)
    current = check.read_version(root)
    if number <= _read_number(current):
        raise RuntimeError(f"{version} does not come after {current}, the version pyproject.toml holds")
    # A branch whose pyproject.toml is behind the tags would otherwise
    # ship a number a release has already gone out under, and the tag
    # for it is free precisely because that release took the other one.
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


def check_upstream(git: str, root: Path, remote: str, ref: str) -> None:
    line = _read_git(git, root, "ls-remote", remote, ref)
    if not line:
        return
    upstream = line.split()[0]
    # A commit this repository does not hold is one this branch cannot
    # hold either, so `merge-base` is read by its exit code and the
    # fatal it prints over an unknown name is not passed on.
    reached = subprocess.run(
        [git, "merge-base", "--is-ancestor", upstream, "HEAD"], cwd=root, check=False, stderr=subprocess.DEVNULL
    )
    if reached.returncode:
        raise RuntimeError(f"{ref} on {remote} is at {upstream}, which this branch does not hold, so a push is refused")


def check_changes(git: str, root: Path, allowed: frozenset[str]) -> None:
    # `uv build` copies the paths it carries off the filesystem, so
    # whatever sits at one of them ships however git reads it: a file
    # no commit holds whatever .gitignore says, or an entry git holds
    # as a link or a gitlink, whose bytes come from wherever it points.
    # A change the index holds and the worktree does not is one the
    # gate never reads and `bump_version` overwrites where it lands on
    # a bumped path. An entry marked assume-unchanged or skip-worktree
    # is one git compares to neither side.
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


def bump_version(uv: str, root: Path, version: str) -> None:
    current = check.read_version(root)
    subprocess.run([uv, "version", version, "--no-sync"], cwd=root, check=True)
    for path, spelling in check.VERSION_COPIES.items():
        copy = root / path
        text = copy.read_text(encoding="utf-8")
        bumped = text.replace(spelling.format(version=current), spelling.format(version=version))
        copy.write_text(bumped, encoding="utf-8")


def _find_unheld(git: str, root: Path, carried: tuple[str, ...]) -> Iterator[str]:
    # `-z` and a format of its own leave the name whole, where the
    # quoting git falls back on writes a path nothing answers to.
    records = _read_git(
        git, root, "ls-files", "-z", "--format=%(objectmode) %(objectname) %(path)", "--", *carried
    ).split("\0")
    entries = [record.split(" ", 2) for record in records if record]
    yield from (path for mode, _, path in entries if mode not in REGULAR_MODES)
    # `--no-filters` hashes the bytes on disk, which are the bytes the
    # build copies: a `.gitattributes` filter, a working-tree encoding
    # and a second name for one inode each leave a worktree git calls
    # clean over a blob holding something else. A path git holds as a
    # regular file and the filesystem does not is a deletion or a
    # typechange the diffs above already name, so hashing it would only
    # fail.
    regular = [entry for entry in entries if entry[0] in REGULAR_MODES and (root / entry[2]).is_file()]
    hashed = _read_git(git, root, "hash-object", "--no-filters", "--", *(entry[2] for entry in regular)).splitlines()
    yield from (entry[2] for entry, blob in zip(regular, hashed, strict=True) if entry[1] != blob)


def _read_carried(root: Path) -> tuple[str, ...]:
    # The wheel takes the module off `src`, and the sdist adds
    # pyproject.toml, the readme and the licences -- which the wheel
    # carries too, in its metadata and beside it.
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    licences = [
        path.relative_to(root).as_posix() for pattern in project["license-files"] for path in root.glob(pattern)
    ]
    return ("src", "pyproject.toml", project["readme"], *licences)


def _read_releases(git: str, root: Path, remote: str) -> list[tuple[int, ...]]:
    # A tag is a release wherever it sits: one this repository has not
    # pushed yet and one it has never fetched both name a version that
    # is taken. `ls-remote` writes `<oid>\trefs/tags/<tag>`, and an
    # annotated tag earns a second line for the commit it peels to.
    tags = (
        *_read_git(git, root, "tag", "--list", f"{TAG_PREFIX}*").splitlines(),
        *_read_git(git, root, "ls-remote", "--tags", remote, f"{TAG_PREFIX}*").splitlines(),
    )
    numbers = {line.rpartition("/")[2].removesuffix("^{}").removeprefix(TAG_PREFIX) for line in tags}
    return [_read_number(number) for number in numbers if NUMBER_PATTERN.fullmatch(number)]


def _read_number(version: str) -> tuple[int, ...]:
    if not NUMBER_PATTERN.fullmatch(version):
        raise RuntimeError(f"{version} is not `major.minor.patch`, the way releases here are numbered")
    return tuple(int(part) for part in version.split("."))


def _read_git(git: str, root: Path, *arguments: str) -> str:
    # Git writes what it refuses to do to stderr, so only its answer is
    # taken and its complaints stay on the terminal.
    return subprocess.run(
        [git, *arguments], cwd=root, check=True, stdout=subprocess.PIPE, encoding="utf-8"
    ).stdout.strip()


if __name__ == "__main__":
    main()
