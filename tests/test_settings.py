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


def test_creates_a_complete_self_documenting_configuration(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)

    config_file = tmp_path / paths.CONFIG_FILE
    config = yaml.safe_load(config_file.read_text())
    assert config_file.read_text() == CONFIG_TEMPLATE
    assert "#" in config_file.read_text()
    assert config["llm"] == {"provider": "openai-subscription"}
    assert config["logging"] == {"level": "INFO"}
    assert {name: agent["model"] for name, agent in config["agents"].items()} == {
        "interviewer": "gpt-5.6-sol",
        "explorer": "gpt-5.6-terra",
        "functional_analyst": "gpt-5.6-sol",
        "architect": "gpt-5.6-sol",
    }


def test_preserves_existing_files_and_appends_ignores_idempotently(tmp_path: Path) -> None:
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


def test_takes_every_default_from_the_generated_configuration(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)
    template = yaml.safe_load(CONFIG_TEMPLATE)

    settings = Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]

    assert settings.llm.provider == template["llm"]["provider"]
    assert settings.agents.model_dump(exclude_none=True) == template["agents"]
    assert settings.logging.level == template["logging"]["level"]


def test_reports_an_incomplete_configuration(tmp_path: Path) -> None:
    config = yaml.safe_load(CONFIG_TEMPLATE)
    del config["agents"]["explorer"]["model"]
    del config["logging"]
    write_config(tmp_path, config)

    with pytest.raises(ValidationError, match=r"agents\.explorer\.model[\s\S]*logging"):
        Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]


def test_leaves_omitted_model_options_to_the_model(tmp_path: Path) -> None:
    config = yaml.safe_load(CONFIG_TEMPLATE)
    config["agents"]["explorer"].pop("reasoning_effort", None)
    config["agents"]["explorer"].pop("temperature", None)
    write_config(tmp_path, config)

    settings = Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]

    assert settings.agents.explorer.reasoning_effort is None
    assert settings.agents.explorer.temperature is None


def test_reads_api_keys_from_the_environment_variables_they_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_reports_an_unset_environment_variable(tmp_path: Path) -> None:
    config = yaml.safe_load(CONFIG_TEMPLATE)
    config["llm"] = {"provider": "https://api.openai.com/v1", "api_key": "MISSING_API_KEY"}
    write_config(tmp_path, config)

    with pytest.raises(ValidationError, match="MISSING_API_KEY, but that environment variable is not set"):
        Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]


def test_requires_an_api_key_without_a_subscription(tmp_path: Path) -> None:
    config = yaml.safe_load(CONFIG_TEMPLATE)
    config["llm"] = {"provider": "https://api.openai.com/v1"}
    write_config(tmp_path, config)

    with pytest.raises(ValidationError, match=r"llm\.api_key"):
        Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]


def test_resolves_cli_over_environment_over_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = yaml.safe_load(CONFIG_TEMPLATE)
    config["llm"] = {"provider": "https://api.openai.com/v1", "api_key": "PROVIDER_API_KEY"}
    config["agents"]["interviewer"] |= {"model": "file-model", "reasoning_effort": "low"}
    write_config(tmp_path, config)
    monkeypatch.setenv("PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("JRI_AGENTS_INTERVIEWER_MODEL", "environment-model")
    monkeypatch.setenv("JRI_AGENTS_INTERVIEWER_REASONING_EFFORT", "medium")

    settings = Settings(
        cwd=tmp_path,
        _cli_parse_args=["--agents.interviewer.model", "cli-model"],  # pyright: ignore[reportCallIssue]
    )

    assert settings.llm.provider == "https://api.openai.com/v1"
    assert settings.llm.api_key == "PROVIDER_API_KEY"
    assert settings.agents.interviewer.model == "cli-model"
    assert settings.agents.interviewer.reasoning_effort == "medium"
