#!/usr/bin/env -S uv run --script

import re
import shutil
import subprocess
from argparse import ArgumentParser
from pathlib import Path

import check

COMMIT_MESSAGE = "chore: version"
NUMBER_PATTERN = re.compile(r"\d+\.\d+\.\d+")
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
    check_number(root, version)
    remote = read_remote(git, root)
    check_tag(git, root, remote, version)
    check_changes(git, root, frozenset())
    bump_version(uv, root, version)
    try:
        # publish.yml gates a release on this exact run, so a release
        # that cannot pass it is stopped here rather than on the tag.
        check.check_project(root, contracts=True)
        # The gate lets `ruff --fix` rewrite what it fixes, and a commit
        # of the version alone would push a tree nothing checked.
        check_changes(git, root, frozenset(BUMPED_PATHS))
    except (RuntimeError, subprocess.CalledProcessError):
        # The bump is this script's own edit, so a release turned down
        # after it takes the edit back out.
        subprocess.run([git, "restore", "--", *BUMPED_PATHS], cwd=root, check=True)
        raise
    subprocess.run([git, "add", "--", *BUMPED_PATHS], cwd=root, check=True)
    subprocess.run([git, "commit", "--message", COMMIT_MESSAGE], cwd=root, check=True)
    subprocess.run([git, "push"], cwd=root, check=True)


def check_number(root: Path, version: str) -> None:
    current = check.read_version(root)
    if _read_number(version) <= _read_number(current):
        raise RuntimeError(f"{version} does not come after {current}, the version pyproject.toml holds")


def read_remote(git: str, root: Path) -> str:
    branch = _read_git(git, root, "branch", "--show-current")
    if not branch:
        raise RuntimeError("HEAD is detached, so there is no branch to push")
    remote = _read_git(git, root, "for-each-ref", "--format=%(upstream:remotename)", f"refs/heads/{branch}")
    if not remote:
        raise RuntimeError(f"{branch} tracks nothing, so a push has nowhere to go")
    return remote


def check_tag(git: str, root: Path, remote: str, version: str) -> None:
    tag = f"{TAG_PREFIX}{version}"
    if _read_git(git, root, "tag", "--list", tag) or _read_git(git, root, "ls-remote", "--tags", remote, tag):
        raise RuntimeError(f"{tag} exists already, so {version} is a release that has gone out")


def check_changes(git: str, root: Path, allowed: frozenset[str]) -> None:
    # An untracked file under `src` is built into the wheel and lands
    # in no commit, so it counts as a change like any other.
    changed = {
        *_read_git(git, root, "diff", "--name-only", "HEAD").splitlines(),
        *_read_git(git, root, "ls-files", "--others", "--exclude-standard").splitlines(),
    }
    if unexpected := sorted(changed - allowed):
        raise RuntimeError("A release goes out of a tree holding nothing else:\n" + "\n".join(unexpected))


def bump_version(uv: str, root: Path, version: str) -> None:
    current = check.read_version(root)
    subprocess.run([uv, "version", version, "--no-sync"], cwd=root, check=True)
    for path, spelling in check.VERSION_COPIES.items():
        copy = root / path
        text = copy.read_text(encoding="utf-8")
        bumped = text.replace(spelling.format(version=current), spelling.format(version=version))
        copy.write_text(bumped, encoding="utf-8")


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
