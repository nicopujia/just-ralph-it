import os
from pathlib import Path
from typing import Annotated, Any, Literal, cast, override

from openai import OpenAI
from openai.types.shared import ReasoningEffort
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

from jri.core import paths
from jri.lib.providers import codex

type LoggingLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
type Temperature = Annotated[float, Field(ge=0, le=2)] | None
CONFIG_TEMPLATE = """\
# Every setting below can also be given as an environment variable following its path
# (JRI_LLM_PROVIDER, JRI_AGENTS_INTERVIEWER_MODEL, ...) or as a CLI flag (see `jri --help`).

llm:
  # Either "openai-subscription", to reuse a ChatGPT subscription through the Codex CLI,
  # or the base URL of any OpenAI-compatible provider, such as https://api.openai.com/v1.
  #
  # The subscription needs the Codex CLI (https://learn.chatgpt.com/docs/codex/cli) to store its
  # credentials in a file, so set `cli_auth_credentials_store = "file"` in ~/.codex/config.toml
  # and run `codex login`.
  #
  provider: openai-subscription

  # Name of the environment variable holding the API key of the provider above.
  # Required unless the provider is "openai-subscription".
  # NEVER put the key itself here: JRI reads it from your shell and from the .env file at the root of your project.
  #
  # api_key: OPENAI_API_KEY

# Web search for the explorer agent, on top of the shell, files, and URLs it always has.
# Get a key at https://brave.com/search/api/ and name its environment variable here.
#
# brave_search:
#   api_key: BRAVE_SEARCH_API_KEY

# Each agent selects a model available on the provider above, a reasoning effort (minimal, low,
# medium, high, or xhigh), and a sampling temperature (0 = focused, 2 = varied).
# Omit the reasoning effort on models without reasoning, and the temperature to let the model
# pick it; reasoning models reject the temperature outright.
agents:
  # Leads the requirements gathering interview.
  # Recommended model type: smart yet relatively fast.
  interviewer:
    model: gpt-5.6-sol
    reasoning_effort: medium

  # Runs shell commands, reads files, and browses the web on the interviewer's behalf.
  # Recommended model type: low cost, fast and with vision capabilities.
  explorer:
    model: gpt-5.6-terra
    reasoning_effort: low
    temperature: 0

  # Turns the interview notes into functional specifications.
  # Recommended model type: as smart as possible.
  functional_analyst:
    model: gpt-5.6-sol
    reasoning_effort: xhigh

  # Designs the system that satisfies those specifications.
  # Recommended model type: as smart as possible.
  architect:
    model: gpt-5.6-sol
    reasoning_effort: xhigh

logging:
  # One of DEBUG, INFO, WARNING, ERROR, or CRITICAL. Logs are written to .jri/logs/.
  level: INFO
"""


def initialize_workspace(cwd: Path) -> None:
    """Create the default configuration and the workspace ignores."""

    workspace = cwd / paths.WORKSPACE_DIR
    workspace.mkdir(exist_ok=True, parents=True)
    config_file = cwd / paths.CONFIG_FILE
    if not config_file.exists():
        config_file.write_text(CONFIG_TEMPLATE)

    ignored = (paths.SESSION_FILE, paths.LOGS_DIR, paths.VISUALIZATION_FILE)
    gitignore = cwd / paths.GITIGNORE_FILE
    content = gitignore.read_text() if gitignore.exists() else ""
    missing = [Path(path).name for path in ignored if Path(path).name not in content.splitlines()]
    if missing:
        separator = "" if not content or content.endswith("\n") else "\n"
        gitignore.write_text(f"{content}{separator}{'\n'.join(missing)}\n")


class Agent(BaseModel):
    """Model configuration for an agent."""

    model: str = Field(description="Model ID.")
    reasoning_effort: ReasoningEffort = Field(
        default=None, description="Model reasoning effort, or omitted for models without reasoning."
    )
    temperature: Temperature = Field(
        default=None, description="Model sampling temperature, or omitted for the model's own."
    )


class Agents(BaseSettings):
    """Model configuration of every agent."""

    interviewer: Agent
    explorer: Agent
    functional_analyst: Agent
    architect: Agent

    model_config = SettingsConfigDict(
        env_prefix="JRI_AGENTS_", env_nested_delimiter="_", env_nested_max_split=2, extra="ignore"
    )


def read_api_key(variable: str) -> str:
    """Read the API key held by the named environment variable.

    Returns:
        The API key.
    """

    return os.environ[variable]


class LLM(BaseSettings):
    """LLM provider configuration."""

    provider: str = Field(
        description=(
            "Set to openai-subscription to use an existing Codex ChatGPT login, set to an OpenAI-compatible base "
            "URL to use that provider with api_key."
        )
    )
    api_key: str | None = Field(
        default=None, description="Name of the environment variable holding the LLM provider's API key."
    )

    model_config = SettingsConfigDict(env_prefix="JRI_LLM_", extra="ignore")

    @property
    def client(self) -> OpenAI:
        """Build a client for the configured provider."""

        if self.provider == "openai-subscription":
            return codex.Client()
        return OpenAI(base_url=self.provider, api_key=read_api_key(cast("str", self.api_key)))

    def validate_authentication(self) -> None:
        """Validate subscription authentication when configured."""

        if self.provider == "openai-subscription":
            codex.Auth().validate()


class BraveSearch(BaseSettings):
    """Brave Search configuration."""

    api_key: str | None = Field(
        default=None, description="Name of the environment variable holding the Brave Search LLM Context API key."
    )

    model_config = SettingsConfigDict(env_prefix="JRI_BRAVE_SEARCH_", extra="ignore")


class Logging(BaseSettings):
    """Application logging configuration."""

    level: LoggingLevel = Field(description=f"Minimum logging level for logs saved under {paths.LOGS_DIR}/.")

    model_config = SettingsConfigDict(env_prefix="JRI_LOGGING_", extra="ignore")


_RUNTIME_FIELDS = frozenset({"cwd", "force"})


def _build_file_schema(model: type[BaseModel]) -> type[BaseModel]:
    """Build the schema the configuration file is allowed to set.

    Every field is optional so the file may leave settings to the other
    sources, and unknown keys are rejected.

    Returns:
        A model mirroring the settings the file may define.
    """

    fields: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        annotation: Any = field.annotation
        if name in _RUNTIME_FIELDS:
            continue
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            section = _build_file_schema(annotation)
            if section.model_fields:
                fields[name] = (section | None, None)
        else:
            fields[name] = (annotation | None, None)
    return create_model(f"{model.__name__}File", __config__=ConfigDict(extra="forbid"), **fields)


class _YamlSource(YamlConfigSettingsSource):
    def __init__(self, settings_cls: type[BaseSettings], file: str, schema: type[BaseModel]) -> None:
        self.schema = schema
        super().__init__(settings_cls, yaml_file=file, yaml_file_encoding="utf-8")

    @override
    def __call__(self) -> dict[str, Any]:
        return self.schema.model_validate(super().__call__()).model_dump(exclude_unset=True)


class Settings(BaseSettings):
    """Settings loaded from project files, CLI, and environment."""

    cwd: Path = Field(description="Current working directory.", default_factory=Path.cwd)
    force: bool = Field(description="Force re-creation of the JRI workspace.", default=False)
    llm: LLM
    brave_search: BraveSearch = Field(default_factory=BraveSearch)
    agents: Agents
    logging: Logging

    model_config = SettingsConfigDict(
        cli_kebab_case=True,
        cli_parse_args=True,
        cli_implicit_flags="toggle",
        cli_avoid_json=True,
        env_prefix="JRI_",
        env_nested_delimiter="_",
        env_nested_max_split=2,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    @override
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Load the project configuration last, so overrides win.

        Returns:
            The ordered settings sources.
        """

        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSource(settings_cls, paths.CONFIG_FILE, _build_file_schema(cls)),
        )

    @model_validator(mode="after")
    def validate_api_keys(self) -> "Settings":
        """Require API keys to be readable from the environment.

        Returns:
            The validated settings.

        Raises:
            ValueError: Raised when an API key is missing or unset.
        """

        if self.llm.provider != "openai-subscription" and not self.llm.api_key:
            raise ValueError(
                "llm.api_key must name the environment variable holding the API key, "
                "unless llm.provider is openai-subscription"
            )
        for section, variable in (("llm", self.llm.api_key), ("brave_search", self.brave_search.api_key)):
            if variable and variable not in os.environ:
                raise ValueError(f"{section}.api_key names {variable}, but that environment variable is not set")
        return self
