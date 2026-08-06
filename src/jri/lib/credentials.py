from pathlib import Path

__all__ = ["holds_credentials"]

# Where a developer machine keeps its secrets, by the convention each
# name answers to rather than by what any one project does with it:
# environment files, private keys, registry logins, credential stores,
# and the two directories whose every file is key material. Only names
# whose whole purpose is to hold a credential belong here; a config
# file that happens to cache a token is a race no list of names wins.
PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "id_rsa",
    "id_ed25519",
    ".netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    ".codex/auth.json",
    ".aws/**",
    ".ssh/**",
)


def holds_credentials(path: Path) -> bool:
    # A symlink is two names for one file, and either of them naming a
    # credential settles it: following the link out of a `.env` hides
    # the contents as surely as pointing a plain name into one.
    # A case-insensitive filesystem answers `.ENV` with the `.env` it
    # holds, so a name differing only in case names that same file.
    # A hard link is two names for one file that no name here can see,
    # and it stays open and stated, like `run_shell`: the question it
    # asks is whether some credential name shares this file's inode,
    # and nothing maps an inode back to the names it answers to.
    # Hunting the twin answers a demonstration rather than the class,
    # since the link can be made from any directory on the filesystem,
    # and refusing every file that has a second name refuses on a
    # property no secret has -- the one a package manager hands every
    # installed file wherever the filesystem will not clone.
    return any(
        candidate.full_match(f"**/{pattern}", case_sensitive=False)
        for candidate in (path, path.resolve())
        for pattern in PATTERNS
    )
