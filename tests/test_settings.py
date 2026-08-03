import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from jri.core import paths
from jri.core.settings import CONFIG_TEMPLATE, Settings, initialize_workspace


@pytest.fixture(autouse=True)
def isolate_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith("JRI_"):
            monkeypatch.delenv(name)


def write_config(tmp_path: Path, config: dict[str, Any]) -> None:
    config_file = tmp_path / paths.CONFIG_FILE
    config_file.parent.mkdir(exist_ok=True)
    config_file.write_text(yaml.safe_dump(config))


def test_initialize_workspace_creates_complete_self_documenting_configuration(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)

    config_file = tmp_path / paths.CONFIG_FILE
    assert config_file.read_text() == CONFIG_TEMPLATE
    assert yaml.safe_load(config_file.read_text()) == {
        "llm": {"provider": "openai-subscription"},
        "agents": {
            "interviewer": {"model": "gpt-5.6-sol", "reasoning_effort": "medium"},
            "explorer": {"model": "gpt-5.6-terra", "reasoning_effort": "low", "temperature": 0},
            "functional_analyst": {"model": "gpt-5.6-sol", "reasoning_effort": "xhigh"},
            "architect": {"model": "gpt-5.6-sol", "reasoning_effort": "xhigh"},
        },
        "logging": {"level": "INFO"},
    }
    assert "#" in config_file.read_text()


def test_initialize_workspace_preserves_files_and_appends_ignores_idempotently(tmp_path: Path) -> None:
    workspace = tmp_path / paths.WORKSPACE_DIR
    workspace.mkdir()
    config_file = tmp_path / paths.CONFIG_FILE
    gitignore_file = tmp_path / paths.GITIGNORE_FILE
    config_file.write_text("custom config\n")
    gitignore_file.write_text("custom-cache\nlogs")

    initialize_workspace(tmp_path)
    initialize_workspace(tmp_path)

    assert config_file.read_text() == "custom config\n"
    assert gitignore_file.read_text() == "custom-cache\nlogs\nsession.json\nvisualization.html\n"


def test_generated_configuration_is_the_only_source_of_defaults(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)
    template = yaml.safe_load(CONFIG_TEMPLATE)

    settings = Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]

    assert settings.llm.provider == template["llm"]["provider"]
    assert settings.agents.model_dump(exclude_none=True) == template["agents"]
    assert settings.logging.level == template["logging"]["level"]


def test_incomplete_configuration_is_reported(tmp_path: Path) -> None:
    config = yaml.safe_load(CONFIG_TEMPLATE)
    del config["agents"]["explorer"]["model"]
    del config["logging"]
    write_config(tmp_path, config)

    with pytest.raises(ValidationError, match=r"agents\.explorer\.model[\s\S]*logging"):
        Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]


def test_omitted_model_options_are_left_to_the_model(tmp_path: Path) -> None:
    config = yaml.safe_load(CONFIG_TEMPLATE)
    del config["agents"]["explorer"]["reasoning_effort"]
    del config["agents"]["explorer"]["temperature"]
    write_config(tmp_path, config)

    settings = Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]

    assert settings.agents.explorer.reasoning_effort is None
    assert settings.agents.explorer.temperature is None


def test_api_keys_name_environment_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = yaml.safe_load(CONFIG_TEMPLATE)
    config["llm"] = {"provider": "https://api.openai.com/v1", "api_key": "PROVIDER_API_KEY"}
    config["brave_search"] = {"api_key": "SEARCH_API_KEY"}
    write_config(tmp_path, config)
    monkeypatch.setenv("PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SEARCH_API_KEY", "search-key")

    settings = Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]

    assert settings.llm.api_key == "PROVIDER_API_KEY"
    assert settings.brave_search.api_key == "SEARCH_API_KEY"
    assert settings.llm.client.api_key == "provider-key"


def test_unset_environment_variable_is_reported(tmp_path: Path) -> None:
    config = yaml.safe_load(CONFIG_TEMPLATE)
    config["llm"] = {"provider": "https://api.openai.com/v1", "api_key": "MISSING_API_KEY"}
    write_config(tmp_path, config)

    with pytest.raises(ValidationError, match="MISSING_API_KEY, but that environment variable is not set"):
        Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]


def test_missing_api_key_is_required_without_a_subscription(tmp_path: Path) -> None:
    config = yaml.safe_load(CONFIG_TEMPLATE)
    config["llm"] = {"provider": "https://api.openai.com/v1"}
    write_config(tmp_path, config)

    with pytest.raises(ValidationError, match=r"llm\.api_key"):
        Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]


def test_settings_resolve_cli_environment_and_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = yaml.safe_load(CONFIG_TEMPLATE)
    config["llm"] = {"provider": "https://api.openai.com/v1", "api_key": "PROVIDER_API_KEY"}
    config["agents"]["interviewer"] |= {"model": "file-model", "reasoning_effort": "low"}
    write_config(tmp_path, config)
    monkeypatch.setenv("PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("JRI_AGENTS_INTERVIEWER_MODEL", "environment-model")
    monkeypatch.setenv("JRI_AGENTS_INTERVIEWER_REASONING_EFFORT", "medium")

    settings = Settings(  # pyright: ignore[reportCallIssue]
        cwd=tmp_path, _cli_parse_args=["--agents.interviewer.model", "cli-model"]
    )

    assert settings.llm.provider == "https://api.openai.com/v1"
    assert settings.llm.api_key == "PROVIDER_API_KEY"
    assert settings.agents.interviewer.model == "cli-model"
    assert settings.agents.interviewer.reasoning_effort == "medium"
