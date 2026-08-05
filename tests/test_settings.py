import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from jri.core import paths
from jri.core.settings import Settings
from jri.lib.providers import codex
from tests.doubles.codex import DISTANT_FUTURE, build_token, write_login

SETTING_PATTERN = re.compile(r"(# )?[a-z_]+:( .*)?")


def write_config(tmp_path: Path, config: dict[str, Any]) -> None:
    write_config_text(tmp_path, yaml.safe_dump(config))


def write_config_text(tmp_path: Path, text: str) -> None:
    config_file = tmp_path / paths.CONFIG_FILE
    config_file.parent.mkdir(exist_ok=True)
    config_file.write_text(text)


def test_generates_a_configuration_that_round_trips_through_the_settings(tmp_path: Path) -> None:
    (tmp_path / paths.CONFIG_FILE).parent.mkdir(exist_ok=True)
    (tmp_path / paths.CONFIG_FILE).write_text(Settings.render_config())

    settings = Settings.load()

    assert settings.model_dump() == Settings().model_dump()
    assert settings.llm.provider == "openai-subscription"
    assert settings.logging.level == "INFO"
    assert {name: agent["model"] for name, agent in settings.agents.model_dump().items()} == {
        "interviewer": "gpt-5.6-sol",
        "explorer": "gpt-5.6-terra",
        "functional_analyst": "gpt-5.6-sol",
        "architect": "gpt-5.6-sol",
    }
    assert {name: agent["reasoning_effort"] for name, agent in settings.agents.model_dump().items()} == {
        "interviewer": "medium",
        "explorer": "low",
        "functional_analyst": "xhigh",
        "architect": "xhigh",
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
        Settings.load()


def test_reports_a_setting_it_does_not_know(tmp_path: Path) -> None:
    config = yaml.safe_load(Settings.render_config())
    config["agents"]["designer"] = {"model": "gpt-5.6-sol"}
    write_config(tmp_path, config)

    with pytest.raises(ValidationError, match=r"agents\.designer"):
        Settings.load()


def test_suggests_the_setting_a_mistyped_key_resembles() -> None:
    assert Settings.suggest_setting(("llm", "provder")) == "llm.provider"
    assert Settings.suggest_setting(("lgging",)) == "logging"
    assert Settings.suggest_setting(("brave_search", "api-key")) == "brave_search.api_key"


def test_suggests_the_setting_an_abbreviated_key_begins() -> None:
    assert Settings.suggest_setting(("agents", "explorer", "temp")) == "agents.explorer.temperature"
    assert Settings.suggest_setting(("agents", "explorer", "reasoning")) == "agents.explorer.reasoning_effort"


def test_suggests_nothing_for_a_key_no_setting_resembles() -> None:
    assert Settings.suggest_setting(("agents", "designer")) is None
    assert Settings.suggest_setting(("nowhere", "provder")) is None


def test_suggests_nothing_for_a_key_several_settings_begin() -> None:
    assert Settings.suggest_setting(("l",)) is None


def test_leaves_omitted_model_options_to_the_model(tmp_path: Path) -> None:
    config = yaml.safe_load(Settings.render_config())
    config["agents"]["explorer"].pop("reasoning_effort", None)
    config["agents"]["explorer"].pop("temperature", None)
    write_config(tmp_path, config)

    settings = Settings.load()

    assert settings.agents.explorer.reasoning_effort is None
    assert settings.agents.explorer.temperature is None


def test_rejects_a_reasoning_effort_it_does_not_document(tmp_path: Path) -> None:
    config = yaml.safe_load(Settings.render_config())
    config["agents"]["interviewer"]["reasoning_effort"] = "maximum"
    write_config(tmp_path, config)

    with pytest.raises(ValidationError, match=r"agents\.interviewer\.reasoning_effort"):
        Settings.load()


def test_reads_api_keys_from_the_environment_variables_they_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = yaml.safe_load(Settings.render_config())
    config["llm"] = {"provider": "https://api.openai.com/v1", "api_key": "PROVIDER_API_KEY"}
    config["brave_search"] = {"api_key": "SEARCH_API_KEY"}
    write_config(tmp_path, config)
    monkeypatch.setenv("PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SEARCH_API_KEY", "search-key")

    settings = Settings.load()

    assert settings.llm.api_key == "PROVIDER_API_KEY"
    assert settings.brave_search.api_key == "SEARCH_API_KEY"
    assert settings.llm.client.api_key == "provider-key"


def test_reports_an_unset_environment_variable(tmp_path: Path) -> None:
    config = yaml.safe_load(Settings.render_config())
    config["llm"] = {"provider": "https://api.openai.com/v1", "api_key": "MISSING_API_KEY"}
    write_config(tmp_path, config)

    with pytest.raises(ValidationError, match="MISSING_API_KEY, but that environment variable is not set"):
        Settings.load()


def test_requires_an_api_key_without_a_subscription(tmp_path: Path) -> None:
    config = yaml.safe_load(Settings.render_config())
    config["llm"] = {"provider": "https://api.openai.com/v1"}
    write_config(tmp_path, config)

    with pytest.raises(ValidationError, match=r"llm\.api_key"):
        Settings.load()


def test_falls_back_to_the_defaults_for_a_blank_configuration_file(tmp_path: Path) -> None:
    write_config_text(tmp_path, "  \n\n")

    settings = Settings.load()

    assert settings.model_dump() == Settings().model_dump()


def test_reports_a_configuration_file_that_is_not_yaml(tmp_path: Path) -> None:
    write_config_text(tmp_path, "llm: [unclosed\n")

    with pytest.raises(yaml.YAMLError):
        Settings.load()


def test_reports_a_configuration_file_that_is_not_a_mapping(tmp_path: Path) -> None:
    write_config_text(tmp_path, "- llm\n- logging\n")

    with pytest.raises(ValidationError):
        Settings.load()


def test_reports_a_section_without_a_body(tmp_path: Path) -> None:
    write_config_text(tmp_path, "llm:\n")

    with pytest.raises(ValidationError, match="llm"):
        Settings.load()


@pytest.mark.parametrize("temperature", [-0.1, 2.5], ids=["below", "above"])
def test_rejects_a_temperature_outside_the_supported_range(tmp_path: Path, temperature: float) -> None:
    config = yaml.safe_load(Settings.render_config())
    config["agents"]["explorer"]["temperature"] = temperature
    write_config(tmp_path, config)

    with pytest.raises(ValidationError, match=r"agents\.explorer\.temperature"):
        Settings.load()


@pytest.mark.parametrize("temperature", [0, 2], ids=["focused", "varied"])
def test_accepts_the_extremes_of_the_temperature_range(tmp_path: Path, temperature: float) -> None:
    config = yaml.safe_load(Settings.render_config())
    config["agents"]["explorer"]["temperature"] = temperature
    write_config(tmp_path, config)

    assert Settings.load().agents.explorer.temperature == temperature


def test_reports_an_unset_search_environment_variable(tmp_path: Path) -> None:
    config = yaml.safe_load(Settings.render_config())
    config["brave_search"] = {"api_key": "MISSING_SEARCH_API_KEY"}
    write_config(tmp_path, config)

    with pytest.raises(ValidationError, match=r"brave_search\.api_key names MISSING_SEARCH_API_KEY"):
        Settings.load()


def test_reaches_the_subscription_through_the_codex_client(tmp_path: Path) -> None:
    write_config_text(tmp_path, Settings.render_config())

    assert isinstance(Settings.load().llm.client, codex.Client)


def test_accepts_a_complete_subscription_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    write_login(
        tmp_path, {"access_token": build_token(DISTANT_FUTURE), "refresh_token": "refresh", "account_id": "account"}
    )
    write_config_text(tmp_path, Settings.render_config())

    Settings.load().llm.validate_authentication()


def test_reports_a_missing_subscription_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    write_config_text(tmp_path, Settings.render_config())

    with pytest.raises(codex.AuthError):
        Settings.load().llm.validate_authentication()


def test_needs_no_subscription_login_for_another_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PROVIDER_API_KEY", "provider-key")
    config = yaml.safe_load(Settings.render_config())
    config["llm"] = {"provider": "https://api.openai.com/v1", "api_key": "PROVIDER_API_KEY"}
    write_config(tmp_path, config)

    Settings.load().llm.validate_authentication()


def test_takes_every_setting_from_the_configuration_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = yaml.safe_load(Settings.render_config())
    config["agents"]["interviewer"] |= {"model": "file-model", "reasoning_effort": "low"}
    write_config(tmp_path, config)
    monkeypatch.setenv("JRI_AGENTS_INTERVIEWER_MODEL", "environment-model")
    monkeypatch.setenv("JRI_LOGGING_LEVEL", "DEBUG")

    settings = Settings.load()

    assert settings.agents.interviewer.model == "file-model"
    assert settings.agents.interviewer.reasoning_effort == "low"
    assert settings.logging.level == "INFO"
