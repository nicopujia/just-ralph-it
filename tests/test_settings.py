import os
from pathlib import Path

import pytest
import yaml

from jri.core import paths
from jri.core.settings import CONFIG_TEMPLATE, SECRETS_TEMPLATE, Settings, initialize_workspace


@pytest.fixture(autouse=True)
def isolate_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith("JRI_"):
            monkeypatch.delenv(name)


def test_initialize_workspace_creates_complete_self_documenting_configuration(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)

    config_file = tmp_path / paths.CONFIG_FILE
    assert config_file.read_text() == CONFIG_TEMPLATE
    assert yaml.safe_load(config_file.read_text()) == {
        "llm": {"provider": "openai-subscription"},
        "agents": {
            "interviewer": {"model": "gpt-5.6-sol", "reasoning_effort": "high", "temperature": 0.7},
            "explorer": {"model": "gpt-5.6-terra", "reasoning_effort": "low", "temperature": 0},
            "functional_analyst": {"model": "gpt-5.6-sol", "reasoning_effort": "high", "temperature": 0},
            "architect": {"model": "gpt-5.6-sol", "reasoning_effort": "high", "temperature": 0.2},
        },
        "logging": {"level": "INFO"},
    }
    assert "#" in config_file.read_text()
    assert (tmp_path / paths.SECRETS_FILE).read_text() == SECRETS_TEMPLATE


def test_initialize_workspace_preserves_files_and_appends_ignores_idempotently(tmp_path: Path) -> None:
    workspace = tmp_path / paths.WORKSPACE_DIR
    workspace.mkdir()
    config_file = tmp_path / paths.CONFIG_FILE
    secrets_file = tmp_path / paths.SECRETS_FILE
    gitignore_file = tmp_path / paths.GITIGNORE_FILE
    config_file.write_text("custom config\n")
    secrets_file.write_text("custom secrets\n")
    gitignore_file.write_text("custom-cache\nlogs")

    initialize_workspace(tmp_path)
    initialize_workspace(tmp_path)

    assert config_file.read_text() == "custom config\n"
    assert secrets_file.read_text() == "custom secrets\n"
    assert gitignore_file.read_text() == "custom-cache\nlogs\nsecrets.yaml\nsession.json\nvisualization.html\n"


def test_settings_defaults_match_generated_configuration(tmp_path: Path) -> None:
    settings = Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]

    assert settings.llm.provider == "openai-subscription"
    assert settings.agents.interviewer.model_dump() == {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "temperature": 0.7,
    }
    assert settings.agents.explorer.model_dump() == {
        "model": "gpt-5.6-terra",
        "reasoning_effort": "low",
        "temperature": 0,
    }
    assert settings.agents.functional_analyst.model_dump() == {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "temperature": 0,
    }
    assert settings.agents.architect.model_dump() == {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "temperature": 0.2,
    }
    assert settings.logging.level == "INFO"


def test_generated_configuration_does_not_drift_from_the_defaults(tmp_path: Path) -> None:
    settings = Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]
    template = yaml.safe_load(CONFIG_TEMPLATE)

    assert template["llm"]["provider"] == settings.llm.provider
    assert template["agents"] == settings.agents.model_dump()
    assert template["logging"]["level"] == settings.logging.level


def test_partial_agent_configuration_uses_agent_defaults(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)
    (tmp_path / paths.CONFIG_FILE).write_text("""\
agents:
  interviewer:
    model: custom-model
    reasoning_effort: medium
  functional_analyst:
    temperature: 0.2
  architect:
    temperature:
""")

    settings = Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]

    assert settings.agents.interviewer.model_dump() == {
        "model": "custom-model",
        "reasoning_effort": "medium",
        "temperature": 0.7,
    }
    assert settings.agents.functional_analyst.model_dump() == {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "temperature": 0.2,
    }
    assert settings.agents.architect.temperature is None


def test_untouched_secrets_file_leaves_environment_keys_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    initialize_workspace(tmp_path)
    (tmp_path / paths.CONFIG_FILE).write_text("llm:\n  provider: https://api.openai.com/v1\n")
    monkeypatch.setenv("JRI_LLM_API_KEY", "environment-key")
    monkeypatch.setenv("JRI_BRAVE_SEARCH_API_KEY", "environment-search-key")

    settings = Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]

    assert settings.llm.api_key == "environment-key"
    assert settings.brave_search.api_key == "environment-search-key"


def test_settings_resolve_cli_environment_secrets_and_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    initialize_workspace(tmp_path)
    (tmp_path / paths.CONFIG_FILE).write_text("""\
llm:
  provider: https://api.openai.com/v1
agents:
  interviewer:
    model: file-model
    reasoning_effort: low
""")
    (tmp_path / paths.SECRETS_FILE).write_text("""\
llm:
  api_key: file-key
""")
    monkeypatch.setenv("JRI_AGENTS_INTERVIEWER_MODEL", "environment-model")
    monkeypatch.setenv("JRI_AGENTS_INTERVIEWER_REASONING_EFFORT", "medium")
    monkeypatch.setenv("JRI_BRAVE_SEARCH_API_KEY", "search-key")

    settings = Settings(  # pyright: ignore[reportCallIssue]
        cwd=tmp_path, _cli_parse_args=["--agents.interviewer.model", "cli-model"]
    )

    assert settings.llm.provider == "https://api.openai.com/v1"
    assert settings.llm.api_key == "file-key"
    assert settings.brave_search.api_key == "search-key"
    assert settings.agents.interviewer.model == "cli-model"
    assert settings.agents.interviewer.reasoning_effort == "medium"
