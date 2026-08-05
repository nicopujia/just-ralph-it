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
    return any(
        candidate.full_match(f"**/{pattern}", case_sensitive=False)
        for candidate in (path, path.resolve())
        for pattern in PATTERNS
    )
