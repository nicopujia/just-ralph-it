import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from jri.core import paths
from jri.core.settings import AgentProfile, Settings
from jri.lib.providers import codex
from tests.doubles.codex import DISTANT_FUTURE, build_token, write_login

SETTING_PATTERN = re.compile(r"(# )?[a-z_]+:( .*)?")


def write_settings(tmp_path: Path, values: dict[str, Any]) -> None:
    write_settings_text(tmp_path, yaml.safe_dump(values))


def write_settings_text(tmp_path: Path, text: str) -> None:
    settings_file = tmp_path / paths.SETTINGS_FILE
    settings_file.parent.mkdir(exist_ok=True)
    settings_file.write_text(text)


def is_comment(line: str) -> bool:
    # A setting that has no value is also a line that starts with a #.
    return line.strip().startswith("#") and not SETTING_PATTERN.fullmatch(line.strip())


def read_setting_names(model: type[BaseModel]) -> set[str]:
    names = set(model.model_fields)
    for field in model.model_fields.values():
        if isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel):
            names |= read_setting_names(field.annotation)
    return names


def read_comments(lines: list[str]) -> list[str]:
    comments: list[list[str]] = [[]]
    for line in lines:
        if is_comment(line):
            comments[-1].append(line.strip().removeprefix("#").strip())
        elif comments[-1]:
            comments.append([])
    return [" ".join(text for text in comment if text) for comment in comments if comment]


def test_generates_a_settings_file_that_round_trips_through_the_model(tmp_path: Path) -> None:
    (tmp_path / paths.SETTINGS_FILE).parent.mkdir(exist_ok=True)
    (tmp_path / paths.SETTINGS_FILE).write_text(Settings.render())

    settings = Settings.load()

    # The values that the file writes are the values the model reads back. Each one is a default, not a constant
    # this test must repeat.
    assert settings.model_dump() == Settings().model_dump()


def test_generates_a_settings_file_with_no_comments_that_round_trips(tmp_path: Path) -> None:
    text = Settings.render(comments=False)
    write_settings_text(tmp_path, text)

    settings = Settings.load()

    assert settings.model_dump() == Settings().model_dump()
    assert "#" not in text
    # An unset setting has no value to keep, and no comment to name it.
    assert "brave_search" not in text
    assert "temperature" not in text


def test_documents_every_setting_it_generates_one_time() -> None:
    lines = Settings.render().splitlines()

    comments = read_comments(lines)
    documented = {
        line.strip().removeprefix("# ").split(":")[0]
        for index, line in enumerate(lines)
        if SETTING_PATTERN.fullmatch(line.strip()) and is_comment(lines[index - 1])
    }

    # The introduction documents the settings that no section documents.
    assert all(name in comments[0] for name in read_setting_names(Settings) - documented)
    # The agents repeat the settings of the same profile. Only the first agent documents them.
    profile = [str(field.description).replace("\n", " ") for field in AgentProfile.model_fields.values()]
    assert [comment for comment in comments if comment in profile] == profile


def test_reports_incomplete_settings(tmp_path: Path) -> None:
    values = yaml.safe_load(Settings.render())
    del values["agents"]["explorer"]["model"]
    write_settings(tmp_path, values)

    with pytest.raises(ValidationError, match=r"agents\.explorer\.model"):
        Settings.load()


def test_reports_a_setting_it_does_not_know(tmp_path: Path) -> None:
    values = yaml.safe_load(Settings.render())
    values["agents"]["designer"] = {"model": "gpt-5.6-sol"}
    write_settings(tmp_path, values)

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
    values = yaml.safe_load(Settings.render())
    values["agents"]["explorer"].pop("reasoning_effort", None)
    values["agents"]["explorer"].pop("temperature", None)
    write_settings(tmp_path, values)

    settings = Settings.load()

    assert settings.agents.explorer.reasoning_effort is None
    assert settings.agents.explorer.temperature is None


def test_rejects_a_reasoning_effort_it_does_not_document(tmp_path: Path) -> None:
    values = yaml.safe_load(Settings.render())
    values["agents"]["interviewer"]["reasoning_effort"] = "maximum"
    write_settings(tmp_path, values)

    with pytest.raises(ValidationError, match=r"agents\.interviewer\.reasoning_effort"):
        Settings.load()


def test_reads_api_keys_from_the_environment_variables_they_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = yaml.safe_load(Settings.render())
    values["llm"] = {"provider": "https://api.openai.com/v1", "api_key": "PROVIDER_API_KEY"}
    values["brave_search"] = {"api_key": "SEARCH_API_KEY"}
    write_settings(tmp_path, values)
    monkeypatch.setenv("PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SEARCH_API_KEY", "search-key")

    settings = Settings.load()

    assert settings.llm.api_key == "PROVIDER_API_KEY"
    assert settings.brave_search.api_key == "SEARCH_API_KEY"
    assert settings.llm.client.api_key == "provider-key"


def test_reports_an_unset_environment_variable(tmp_path: Path) -> None:
    values = yaml.safe_load(Settings.render())
    values["llm"] = {"provider": "https://api.openai.com/v1", "api_key": "MISSING_API_KEY"}
    write_settings(tmp_path, values)

    with pytest.raises(ValidationError, match="MISSING_API_KEY, but that environment variable is not set"):
        Settings.load()


def test_requires_an_api_key_without_a_subscription(tmp_path: Path) -> None:
    values = yaml.safe_load(Settings.render())
    values["llm"] = {"provider": "https://api.openai.com/v1", "api_key": None}
    write_settings(tmp_path, values)

    with pytest.raises(ValidationError, match=r"llm\.api_key"):
        Settings.load()


def test_falls_back_to_the_defaults_for_a_blank_settings_file(tmp_path: Path) -> None:
    write_settings_text(tmp_path, "  \n\n")

    settings = Settings.load()

    assert settings.model_dump() == Settings().model_dump()


def test_reports_a_settings_file_that_is_not_yaml(tmp_path: Path) -> None:
    write_settings_text(tmp_path, "llm: [unclosed\n")

    with pytest.raises(yaml.YAMLError):
        Settings.load()


def test_reports_a_settings_file_that_is_not_a_mapping(tmp_path: Path) -> None:
    write_settings_text(tmp_path, "- llm\n- logging\n")

    with pytest.raises(ValidationError):
        Settings.load()


def test_reports_a_section_without_a_body(tmp_path: Path) -> None:
    write_settings_text(tmp_path, "llm:\n")

    with pytest.raises(ValidationError, match="llm"):
        Settings.load()


@pytest.mark.parametrize("temperature", [-0.1, 2.5], ids=["below", "above"])
def test_rejects_a_temperature_outside_the_supported_range(tmp_path: Path, temperature: float) -> None:
    values = yaml.safe_load(Settings.render())
    values["agents"]["explorer"]["temperature"] = temperature
    write_settings(tmp_path, values)

    with pytest.raises(ValidationError, match=r"agents\.explorer\.temperature"):
        Settings.load()


@pytest.mark.parametrize("temperature", [0, 2], ids=["focused", "varied"])
def test_accepts_the_extremes_of_the_temperature_range(tmp_path: Path, temperature: float) -> None:
    values = yaml.safe_load(Settings.render())
    values["agents"]["explorer"]["temperature"] = temperature
    write_settings(tmp_path, values)

    assert Settings.load().agents.explorer.temperature == temperature


def test_reports_an_unset_search_environment_variable(tmp_path: Path) -> None:
    values = yaml.safe_load(Settings.render())
    values["brave_search"] = {"api_key": "MISSING_SEARCH_API_KEY"}
    write_settings(tmp_path, values)

    with pytest.raises(ValidationError, match="MISSING_SEARCH_API_KEY, but that environment variable is not set"):
        Settings.load()


def test_blames_the_setting_an_unset_environment_variable_belongs_to(tmp_path: Path) -> None:
    values = yaml.safe_load(Settings.render())
    values["brave_search"] = {"api_key": "MISSING_SEARCH_API_KEY"}
    write_settings(tmp_path, values)

    with pytest.raises(ValidationError) as error:
        Settings.load()

    assert error.value.errors()[0]["loc"] == ("brave_search", "api_key")


def test_blames_the_api_key_a_subscriptionless_provider_needs(tmp_path: Path) -> None:
    values = yaml.safe_load(Settings.render())
    values["llm"] = {"provider": "https://api.openai.com/v1", "api_key": None}
    write_settings(tmp_path, values)

    with pytest.raises(ValidationError) as error:
        Settings.load()

    assert error.value.errors()[0]["loc"] == ("llm", "api_key")


def test_reaches_the_subscription_through_the_codex_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = yaml.safe_load(Settings.render())
    values["llm"] = {"provider": "openai-subscription"}
    write_settings(tmp_path, values)
    # The subscription has its own login. It must not need the variable that llm.api_key names.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert isinstance(Settings.load().llm.client, codex.Client)


def test_accepts_a_complete_subscription_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    write_login(
        tmp_path, {"access_token": build_token(DISTANT_FUTURE), "refresh_token": "refresh", "account_id": "account"}
    )
    values = yaml.safe_load(Settings.render())
    values["llm"] = {"provider": "openai-subscription"}
    write_settings(tmp_path, values)

    Settings.load().llm.validate_authentication()


def test_reports_a_missing_subscription_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    values = yaml.safe_load(Settings.render())
    values["llm"] = {"provider": "openai-subscription"}
    write_settings(tmp_path, values)

    with pytest.raises(codex.AuthError):
        Settings.load().llm.validate_authentication()


def test_needs_no_subscription_login_for_another_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PROVIDER_API_KEY", "provider-key")
    values = yaml.safe_load(Settings.render())
    values["llm"] = {"provider": "https://api.openai.com/v1", "api_key": "PROVIDER_API_KEY"}
    write_settings(tmp_path, values)

    Settings.load().llm.validate_authentication()


# Settings has no generic environment-variable override layer, unlike common 12-factor config tools. Only an
# `api_key` value naming a variable is ever read from the environment, so a `JRI_`-prefixed variable here must
# have no effect.
def test_takes_every_setting_from_the_settings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = yaml.safe_load(Settings.render())
    values["agents"]["interviewer"] |= {"model": "file-model", "reasoning_effort": "low"}
    write_settings(tmp_path, values)
    monkeypatch.setenv("JRI_AGENTS_INTERVIEWER_MODEL", "environment-model")
    monkeypatch.setenv("JRI_LOGGING_LEVEL", "DEBUG")

    settings = Settings.load()

    assert settings.agents.interviewer.model == "file-model"
    assert settings.agents.interviewer.reasoning_effort == "low"
    assert settings.logging.level == "INFO"
