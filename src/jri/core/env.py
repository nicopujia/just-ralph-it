import os
from pathlib import Path


def load_repo_env(root: Path) -> None:
    env_path = root / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        key, separator, value = line.partition("=")
        if separator != "=":
            continue

        name = key.strip()
        if not name or name in os.environ:
            continue

        parsed = value.strip()
        if len(parsed) >= 2 and parsed[0] == parsed[-1] and parsed[0] in {'"', "'"}:
            parsed = parsed[1:-1]
        os.environ[name] = parsed
