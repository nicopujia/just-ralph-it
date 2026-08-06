import os
from pathlib import Path

import pytest

from jri.lib import credentials

SECRET_NAMES = (
    ".env",
    ".env.production",
    "deploy.pem",
    "id_rsa",
    "id_ed25519",
    ".netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    ".codex/auth.json",
    ".aws/config",
    ".ssh/known_hosts",
)
ORDINARY_NAMES = (
    "README.md",
    "pyproject.toml",
    ".gitignore",
    "environment.yml",
    "notes.env.md",
    "credentials.md",
    "src/settings.py",
)


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_finds_credentials_in_the_files_a_machine_keeps_them_in(tmp_path: Path, name: str) -> None:
    assert credentials.holds_credentials(tmp_path / name)


@pytest.mark.parametrize("name", ORDINARY_NAMES)
def test_passes_over_a_file_no_convention_makes_secret(tmp_path: Path, name: str) -> None:
    assert not credentials.holds_credentials(tmp_path / name)


def test_finds_credentials_named_in_another_case(tmp_path: Path) -> None:
    assert credentials.holds_credentials(tmp_path / ".ENV")


def test_finds_credentials_a_plain_name_points_at(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-live-canary\n")
    link = tmp_path / "notes.txt"
    link.symlink_to(tmp_path / ".env")

    assert credentials.holds_credentials(link)


def test_finds_credentials_a_credential_name_points_away_from(tmp_path: Path) -> None:
    (tmp_path / "settings.txt").write_text("OPENAI_API_KEY=sk-live-canary\n")
    link = tmp_path / ".env"
    link.symlink_to(tmp_path / "settings.txt")

    assert credentials.holds_credentials(link)


def test_passes_over_a_hard_link_no_name_tells_from_an_ordinary_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-live-canary\n")
    link = tmp_path / "notes.txt"
    os.link(tmp_path / ".env", link)

    assert not credentials.holds_credentials(link)
