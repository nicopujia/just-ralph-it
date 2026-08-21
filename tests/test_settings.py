import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from jri.core import paths
from jri.core.exceptions import PersistenceError
from jri.core.settings import AgentProfile, Settings
from jri.lib.providers import codex, gateway
from tests.doubles.codex import DISTANT_FUTURE, build_token, write_login

if TYPE_CHECKING:
    from collections.abc import Callable

SETTING_PATTERN = re.compile(r"(# )?[a-z_]+:( .*)?")


# `Settings.load_global` reads the settings of the user who runs the suite. Give each test a home directory of
# its own, so the settings of a machine cannot change a result here.
@pytest.fixture(autouse=True)
def isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "home"
    directory.mkdir()
    monkeypatch.setenv("HOME", str(directory))
    monkeypatch.setenv("USERPROFILE", str(directory))
    return directory


def write_settings(directory: Path, values: dict[str, Any]) -> None:
    write_settings_text(directory, yaml.safe_dump(values))


def write_settings_text(directory: Path, text: str) -> None:
    settings_file = directory / paths.SETTINGS_FILE
    settings_file.parent.mkdir(exist_ok=True)
    settings_file.write_text(text, encoding="utf-8")


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
    (tmp_path / paths.SETTINGS_FILE).write_text(Settings.render(), encoding="utf-8")

    settings = Settings.load()

    # The model reads back the same values that the file writes. Each value is a default, thus this test does
    # not repeat it as a constant.
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
    assert Settings.suggest(("llm", "provder")) == "llm.provider"
    assert Settings.suggest(("lgging",)) == "logging"
    assert Settings.suggest(("brave_search", "api-key")) == "brave_search.api_key"


def test_suggests_the_setting_an_abbreviated_key_begins() -> None:
    assert Settings.suggest(("agents", "explorer", "temp")) == "agents.explorer.temperature"
    assert Settings.suggest(("agents", "explorer", "reasoning")) == "agents.explorer.reasoning_effort"


def test_suggests_nothing_for_a_key_no_setting_resembles() -> None:
    assert Settings.suggest(("agents", "designer")) is None
    assert Settings.suggest(("nowhere", "provder")) is None


def test_suggests_nothing_for_a_key_several_settings_begin() -> None:
    assert Settings.suggest(("l",)) is None


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


def test_needs_no_api_key_for_a_subscription(tmp_path: Path) -> None:
    values = yaml.safe_load(Settings.render())
    # The subscription reads its own login. The user has no key variable to name here.
    values["llm"] = {"provider": "openai-subscription", "api_key": None}
    write_settings(tmp_path, values)

    assert Settings.load().llm.api_key is None


def test_falls_back_to_the_defaults_for_a_blank_settings_file(tmp_path: Path) -> None:
    write_settings_text(tmp_path, "  \n\n")

    settings = Settings.load()

    assert settings.model_dump() == Settings().model_dump()


def test_reports_a_settings_file_that_is_not_yaml(tmp_path: Path) -> None:
    write_settings_text(tmp_path, "llm: [unclosed\n")

    with pytest.raises(yaml.YAMLError, match="while parsing a flow sequence"):
        Settings.load()


def test_reports_a_settings_file_that_is_not_a_mapping(tmp_path: Path) -> None:
    write_settings_text(tmp_path, "- llm\n- logging\n")

    with pytest.raises(ValidationError, match="Input should be a valid dictionary or instance of Settings"):
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

    with pytest.raises(ValidationError, match="but that environment variable is not set") as error:
        Settings.load()

    assert error.value.errors()[0]["loc"] == ("brave_search", "api_key")


def test_blames_the_api_key_a_subscriptionless_provider_needs(tmp_path: Path) -> None:
    values = yaml.safe_load(Settings.render())
    values["llm"] = {"provider": "https://api.openai.com/v1", "api_key": None}
    write_settings(tmp_path, values)

    with pytest.raises(ValidationError, match="must name the environment variable holding the API key") as error:
        Settings.load()

    assert error.value.errors()[0]["loc"] == ("llm", "api_key")


# The gateway takes request fields that no other endpoint knows. Only a client of its own may add them.
def test_reaches_the_gateway_through_the_gateway_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROVIDER_API_KEY", "provider-key")
    values = yaml.safe_load(Settings.render())
    values["llm"] = {"provider": "https://ai-gateway.vercel.sh/v1", "api_key": "PROVIDER_API_KEY"}
    write_settings(tmp_path, values)

    assert isinstance(Settings.load().llm.client, gateway.Client)


def test_reaches_another_provider_through_the_provider_library_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROVIDER_API_KEY", "provider-key")
    values = yaml.safe_load(Settings.render())
    values["llm"] = {"provider": "https://api.openai.com/v1", "api_key": "PROVIDER_API_KEY"}
    write_settings(tmp_path, values)

    assert type(Settings.load().llm.client) is OpenAI


def test_reaches_the_subscription_through_the_codex_client(tmp_path: Path) -> None:
    values = yaml.safe_load(Settings.render())
    # The subscription has its own login. It must not need the variable that llm.api_key names.
    values["llm"] = {"provider": "openai-subscription", "api_key": "MISSING_API_KEY"}
    write_settings(tmp_path, values)

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

    with pytest.raises(codex.AuthError, match="No file-based Codex login found"):
        Settings.load().llm.validate_authentication()


def test_needs_no_subscription_login_for_another_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PROVIDER_API_KEY", "provider-key")
    values = yaml.safe_load(Settings.render())
    values["llm"] = {"provider": "https://api.openai.com/v1", "api_key": "PROVIDER_API_KEY"}
    write_settings(tmp_path, values)

    Settings.load().llm.validate_authentication()


# Settings has no layer that lets the environment replace a value, unlike the usual 12-factor tools. JRI reads
# the environment only for an `api_key` value that names a variable. A `JRI_` variable here does nothing.
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


# A settings file that JRI cannot read holds no setting that a user can fix. Name the file and the reason.
@pytest.mark.skipif(sys.platform == "win32", reason="Windows holds an access list that `chmod` does not write")
@pytest.mark.parametrize("read", [Settings.load, Settings.load_global], ids=["a project", "a home directory"])
def test_reports_a_settings_file_it_could_not_read(
    tmp_path: Path, isolate_home: Path, read: "Callable[[], object]"
) -> None:
    directory = tmp_path if read is Settings.load else isolate_home
    write_settings(directory, {"logging": {"level": "DEBUG"}})
    settings_file = directory / paths.SETTINGS_FILE
    settings_file.chmod(0o000)

    try:
        with pytest.raises(PersistenceError, match="Could not read the settings file"):
            read()
    finally:
        settings_file.chmod(0o600)


def test_starts_a_project_with_the_global_settings(tmp_path: Path, isolate_home: Path) -> None:
    write_settings(isolate_home, {"agents": {"interviewer": {"model": "global/model"}}, "logging": {"level": "DEBUG"}})

    write_settings_text(tmp_path, Settings.render(Settings.load_global()))

    settings = Settings.load()
    assert settings.agents.interviewer.model == "global/model"
    assert settings.logging.level == "DEBUG"


def test_keeps_the_defaults_the_global_settings_leave_out(tmp_path: Path, isolate_home: Path) -> None:
    # A section of the global settings can hold one setting. The rest of that section keeps its default value.
    write_settings(isolate_home, {"agents": {"interviewer": {"temperature": 0.5}}})

    write_settings_text(tmp_path, Settings.render(Settings.load_global()))

    settings = Settings.load()
    assert settings.agents.interviewer.temperature == pytest.approx(0.5)
    assert settings.agents.interviewer.model == Settings().agents.interviewer.model
    assert settings.agents.interviewer.reasoning_effort == Settings().agents.interviewer.reasoning_effort
    assert settings.agents.explorer.model == Settings().agents.explorer.model


@pytest.mark.parametrize("text", [None, "  \n\n"], ids=["absent", "blank"])
def test_starts_a_project_with_the_defaults_when_the_global_settings_name_none(
    tmp_path: Path, isolate_home: Path, text: str | None
) -> None:
    if text is not None:
        write_settings_text(isolate_home, text)

    write_settings_text(tmp_path, Settings.render(Settings.load_global()))

    assert Settings.load().model_dump() == Settings().model_dump()


def test_documents_a_project_that_the_global_settings_started(isolate_home: Path) -> None:
    write_settings(isolate_home, {"logging": {"level": "DEBUG"}})

    lines = Settings.render(Settings.load_global()).splitlines()

    # A global setting must not remove documentation. The file keeps the same comments as a file that the
    # defaults fill.
    assert read_comments(lines) == read_comments(Settings.render().splitlines())
    # JRI indents a global setting in its own section, as it indents a setting that the defaults fill.
    assert "  level: DEBUG" in lines


def test_starts_a_project_with_an_api_key_variable_that_no_environment_sets(isolate_home: Path) -> None:
    # `jri init` reads no .env file, so it accepts the name of a variable that only a later `jri chat` reads.
    write_settings(isolate_home, {"brave_search": {"api_key": "MISSING_SEARCH_API_KEY"}})

    assert "api_key: MISSING_SEARCH_API_KEY" in Settings.render(Settings.load_global())


def test_reports_a_setting_the_global_settings_do_not_know(isolate_home: Path) -> None:
    write_settings(isolate_home, {"agents": {"desiner": {"model": "global/model"}}})

    with pytest.raises(ValidationError, match=r"agents\.desiner"):
        Settings.load_global()


def test_reports_global_settings_that_are_not_yaml(isolate_home: Path) -> None:
    write_settings_text(isolate_home, "llm: [unclosed\n")

    with pytest.raises(yaml.YAMLError, match="while parsing a flow sequence"):
        Settings.load_global()


def test_reports_global_settings_that_are_not_a_mapping(isolate_home: Path) -> None:
    write_settings_text(isolate_home, "- llm\n- logging\n")

    with pytest.raises(ValidationError, match="Input should be a valid dictionary or instance of Settings"):
        Settings.load_global()


def test_reports_a_global_section_that_is_not_a_mapping(isolate_home: Path) -> None:
    write_settings_text(isolate_home, "agents: interviewer\n")

    with pytest.raises(ValidationError, match="Input should be a valid dictionary or instance of AgentProfiles"):
        Settings.load_global()


def test_starts_a_project_with_the_global_settings_and_no_comments(tmp_path: Path, isolate_home: Path) -> None:
    write_settings(isolate_home, {"logging": {"level": "DEBUG"}})

    text = Settings.render(Settings.load_global(), comments=False)
    write_settings_text(tmp_path, text)

    assert Settings.load().logging.level == "DEBUG"
    assert "#" not in text


def test_reports_global_settings_that_name_no_api_key_variable(isolate_home: Path) -> None:
    # This setting is wrong, and no environment variable can make it correct. A project that starts with it
    # cannot chat.
    write_settings(isolate_home, {"llm": {"api_key": None}})

    with pytest.raises(ValidationError, match="must name the environment variable holding the API key"):
        Settings.load_global()
