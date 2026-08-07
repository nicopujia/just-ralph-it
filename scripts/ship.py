#!/usr/bin/env -S uv run --script

import os
import re
import shutil
import subprocess
from argparse import ArgumentParser
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
    remote, ref = read_upstream(git, root)
    check_tag(git, root, remote, version)
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


def check_number(root: Path, version: str) -> None:
    current = check.read_version(root)
    if _read_number(version) <= _read_number(current):
        raise RuntimeError(f"{version} does not come after {current}, the version pyproject.toml holds")


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


def check_tag(git: str, root: Path, remote: str, version: str) -> None:
    tag = f"{TAG_PREFIX}{version}"
    if _read_git(git, root, "tag", "--list", tag) or _read_git(git, root, "ls-remote", "--tags", remote, tag):
        raise RuntimeError(f"{tag} exists already, so {version} is a release that has gone out")


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
    # `uv build` copies `src` off the filesystem, so a file under it
    # that no commit holds ships in the wheel however .gitignore reads
    # it. A change the index holds and the worktree does not is one the
    # gate never reads and `bump_version` overwrites where it lands on
    # a bumped path. An entry marked assume-unchanged or skip-worktree
    # is one git compares to neither side.
    changed = {
        *_read_git(git, root, "diff", "--name-only", "HEAD").splitlines(),
        *_read_git(git, root, "diff", "--name-only", "--cached", "HEAD").splitlines(),
        *_read_git(git, root, "ls-files", "--others", "--exclude-standard").splitlines(),
        *_read_git(git, root, "ls-files", "--others", "--", "src", CACHE_PATHSPEC).splitlines(),
        *(line[2:] for line in _read_git(git, root, "ls-files", "-v").splitlines() if not line.startswith("H ")),
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
