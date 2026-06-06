"""Tests for CLI configuration loading."""

from pathlib import Path

from jri.cli.config import CliArgs, load_cli_environment, parse_arguments


def test_parse_arguments_accepts_force_flag() -> None:
    """The CLI parser owns terminal-facing flags."""
    assert parse_arguments(["--force"]) == CliArgs(force=True)


def test_parse_arguments_accepts_short_force_flag() -> None:
    """Short flags map to the same CLI config."""
    assert parse_arguments(["-f"]) == CliArgs(force=True)


def test_cli_environment_loads_pwd_dotenv(tmp_path: Path) -> None:
    """JRI reads a .env file from the process working directory."""
    (tmp_path / ".env").write_text(
        (
            "# local credentials\n"
            "OPENROUTER_API_KEY=from-dotenv\n"
            "BRAVE_SEARCH_API_KEY='quoted brave key'\n"
            'JRI_MODEL_PRESET="cheap"'
        ),
        encoding="utf-8",
    )

    env = load_cli_environment(cwd=tmp_path, environ={})

    assert env["OPENROUTER_API_KEY"] == "from-dotenv"
    assert env["BRAVE_SEARCH_API_KEY"] == "quoted brave key"
    assert env["JRI_MODEL_PRESET"] == "cheap"


def test_cli_environment_supports_export_and_empty_dotenv_values(
    tmp_path: Path,
) -> None:
    """The lightweight .env parser handles common shell-like lines."""
    (tmp_path / ".env").write_text(
        "export JRI_MODEL_PROVIDER=openrouter\nEMPTY=\n=ignored\n",
        encoding="utf-8",
    )

    env = load_cli_environment(cwd=tmp_path, environ={})

    assert env["JRI_MODEL_PROVIDER"] == "openrouter"
    assert env["EMPTY"] == ""
    assert "" not in env


def test_cli_environment_preserves_exported_values(tmp_path: Path) -> None:
    """Exported shell values stay preferred over local .env values."""
    (tmp_path / ".env").write_text(
        "OPENROUTER_API_KEY=from-dotenv\n",
        encoding="utf-8",
    )

    env = load_cli_environment(
        cwd=tmp_path,
        environ={"OPENROUTER_API_KEY": "from-shell"},
    )

    assert env["OPENROUTER_API_KEY"] == "from-shell"
