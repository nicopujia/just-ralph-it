import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from jri.core import paths
from jri.core.settings import Settings

SETTING_PATTERN = re.compile(r"(# )?[a-z_]+:( .*)?")


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


def test_generates_a_configuration_that_round_trips_through_the_settings(tmp_path: Path) -> None:
    defaults = Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]
    (tmp_path / paths.CONFIG_FILE).parent.mkdir(exist_ok=True)
    (tmp_path / paths.CONFIG_FILE).write_text(Settings.render_config())

    settings = Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]

    assert settings.model_dump() == defaults.model_dump()
    assert settings.llm.provider == "openai-subscription"
    assert settings.logging.level == "INFO"
    assert {name: agent["model"] for name, agent in settings.agents.model_dump().items()} == {
        "interviewer": "gpt-5.6-sol",
        "explorer": "gpt-5.6-terra",
        "functional_analyst": "gpt-5.6-sol",
        "architect": "gpt-5.6-sol",
    }


def test_documents_every_setting_it_generates() -> None:
    lines = Settings.render_config().splitlines()

    settings = [(index, line.strip()) for index, line in enumerate(lines) if SETTING_PATTERN.fullmatch(line.strip())]

    assert [line for index, line in settings if not lines[index - 1].strip().startswith("#")] == []
    assert {line.removeprefix("# ").split(":")[0] for _, line in settings} == {
        "agents",
        "api_key",
        "architect",
        "brave_search",
        "explorer",
        "functional_analyst",
        "interviewer",
        "level",
        "llm",
        "logging",
        "model",
        "provider",
        "reasoning_effort",
        "temperature",
    }


def test_reports_an_incomplete_configuration(tmp_path: Path) -> None:
    config = yaml.safe_load(Settings.render_config())
    del config["agents"]["explorer"]["model"]
    write_config(tmp_path, config)

    with pytest.raises(ValidationError, match=r"agents\.explorer\.model"):
        Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]


def test_leaves_omitted_model_options_to_the_model(tmp_path: Path) -> None:
    config = yaml.safe_load(Settings.render_config())
    config["agents"]["explorer"].pop("reasoning_effort", None)
    config["agents"]["explorer"].pop("temperature", None)
    write_config(tmp_path, config)

    settings = Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]

    assert settings.agents.explorer.reasoning_effort is None
    assert settings.agents.explorer.temperature is None


def test_reads_api_keys_from_the_environment_variables_they_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = yaml.safe_load(Settings.render_config())
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
    config = yaml.safe_load(Settings.render_config())
    config["llm"] = {"provider": "https://api.openai.com/v1", "api_key": "MISSING_API_KEY"}
    write_config(tmp_path, config)

    with pytest.raises(ValidationError, match="MISSING_API_KEY, but that environment variable is not set"):
        Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]


def test_requires_an_api_key_without_a_subscription(tmp_path: Path) -> None:
    config = yaml.safe_load(Settings.render_config())
    config["llm"] = {"provider": "https://api.openai.com/v1"}
    write_config(tmp_path, config)

    with pytest.raises(ValidationError, match=r"llm\.api_key"):
        Settings(cwd=tmp_path, _cli_parse_args=[])  # pyright: ignore[reportCallIssue]


def test_resolves_cli_over_environment_over_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = yaml.safe_load(Settings.render_config())
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
