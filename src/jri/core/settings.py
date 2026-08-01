from pathlib import Path
from typing import Any, Literal, override

from openai import OpenAI
from openai.types.shared import ReasoningEffort
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    CliSuppress,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from jri.core import paths
from jri.lib.providers import codex

type LoggingLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
CONFIG_TEMPLATE = """\
llm:
  # Use a ChatGPT subscription. For an API key, replace this with an OpenAI-compatible base URL
  # and set llm.api_key in secrets.yaml.
  provider: openai-subscription

# Each agent selects a model, reasoning effort, and sampling temperature (0 = focused, 2 = varied).
agents:
  interviewer:
    model: gpt-5.6-sol
    reasoning_effort: high
    temperature: 0.7
  explorer:
    model: gpt-5.6-terra
    reasoning_effort: low
    temperature: 0
  functional_analyst:
    model: gpt-5.6-sol
    reasoning_effort: high
    temperature: 0
  architect:
    model: gpt-5.6-sol
    reasoning_effort: high
    temperature: 0.2

logging:
  # One of DEBUG, INFO, WARNING, ERROR, or CRITICAL.
  level: INFO
"""
SECRETS_TEMPLATE = """\
llm:
  api_key:

brave_search:
  api_key:
"""


def initialize_workspace(cwd: Path) -> None:
    """Create default configuration, secrets, and workspace ignores."""

    workspace = cwd / paths.WORKSPACE_DIR
    workspace.mkdir(exist_ok=True, parents=True)
    for file, template in ((cwd / paths.CONFIG_FILE, CONFIG_TEMPLATE), (cwd / paths.SECRETS_FILE, SECRETS_TEMPLATE)):
        if not file.exists():
            file.write_text(template)

    ignored = (paths.SECRETS_FILE, paths.SESSION_FILE, paths.LOGS_DIR, paths.VISUALIZATION_FILE)
    gitignore = cwd / paths.GITIGNORE_FILE
    content = gitignore.read_text() if gitignore.exists() else ""
    missing = [Path(path).name for path in ignored if Path(path).name not in content.splitlines()]
    if missing:
        separator = "" if not content or content.endswith("\n") else "\n"
        gitignore.write_text(f"{content}{separator}{'\n'.join(missing)}\n")


class Agent(BaseModel):
    """Model configuration for an agent."""

    model: str = Field(description="Model ID.")
    reasoning_effort: ReasoningEffort = Field(description="Model reasoning effort.")
    temperature: float = Field(ge=0, le=2, description="Model sampling temperature.")


class Interviewer(Agent):
    """Interviewer model configuration."""

    model: str = "gpt-5.6-sol"
    reasoning_effort: ReasoningEffort = "high"
    temperature: float = 0.7


class Explorer(Agent):
    """Explorer model configuration."""

    model: str = "gpt-5.6-terra"
    reasoning_effort: ReasoningEffort = "low"
    temperature: float = 0


class FunctionalAnalyst(Agent):
    """Functional Analyst model configuration."""

    model: str = "gpt-5.6-sol"
    reasoning_effort: ReasoningEffort = "high"
    temperature: float = 0


class Architect(Agent):
    """Architect model configuration."""

    model: str = "gpt-5.6-sol"
    reasoning_effort: ReasoningEffort = "high"
    temperature: float = 0.2


class Agents(BaseSettings):
    """Agent model configuration."""

    interviewer: Interviewer = Field(default_factory=Interviewer)
    explorer: Explorer = Field(default_factory=Explorer)
    functional_analyst: FunctionalAnalyst = Field(default_factory=FunctionalAnalyst)
    architect: Architect = Field(default_factory=Architect)

    model_config = SettingsConfigDict(
        env_prefix="JRI_AGENTS_", env_nested_delimiter="_", env_nested_max_split=2, extra="ignore"
    )


class LLM(BaseSettings):
    """LLM provider configuration."""

    provider: str = Field(
        default="openai-subscription",
        description=(
            "Set to openai-subscription to use an existing Codex ChatGPT login, set to an OpenAI-compatible base "
            "URL to use that provider with api_key."
        ),
    )
    api_key: CliSuppress[str | None] = Field(default=None, description="API key for the configured LLM provider.")

    model_config = SettingsConfigDict(env_prefix="JRI_LLM_", extra="ignore")

    @property
    def client(self) -> OpenAI:
        """Build a client for the configured provider."""

        if self.provider == "openai-subscription":
            return codex.Client()
        return OpenAI(base_url=self.provider, api_key=self.api_key)

    def validate_authentication(self) -> None:
        """Validate subscription authentication when configured."""

        if self.provider == "openai-subscription":
            codex.Auth().validate()


class BraveSearch(BaseSettings):
    """Brave Search configuration."""

    api_key: CliSuppress[str | None] = Field(default=None, description="Brave Search LLM Context API key.")

    model_config = SettingsConfigDict(env_prefix="JRI_BRAVE_SEARCH_", extra="ignore")


class Logging(BaseSettings):
    """Application logging configuration."""

    level: LoggingLevel = Field(
        default="INFO", description=f"Minimum logging level for logs saved under {paths.LOGS_DIR}/."
    )

    model_config = SettingsConfigDict(env_prefix="JRI_LOGGING_", extra="ignore")


class _FileModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _LLMFile(_FileModel):
    provider: str | None = None


class _LoggingFile(_FileModel):
    level: LoggingLevel | None = None


class _AgentFile(_FileModel):
    model: str | None = None
    reasoning_effort: ReasoningEffort = None
    temperature: float | None = Field(default=None, ge=0, le=2)


class _AgentsFile(_FileModel):
    interviewer: _AgentFile | None = None
    explorer: _AgentFile | None = None
    functional_analyst: _AgentFile | None = None
    architect: _AgentFile | None = None


class _ConfigFile(_FileModel):
    llm: _LLMFile | None = None
    agents: _AgentsFile | None = None
    logging: _LoggingFile | None = None


class _APIKey(_FileModel):
    api_key: str | None = None


class _SecretsFile(_FileModel):
    llm: _APIKey | None = None
    brave_search: _APIKey | None = None


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
    llm: LLM = Field(default_factory=LLM)
    brave_search: BraveSearch = Field(default_factory=BraveSearch)
    agents: Agents = Field(default_factory=Agents)
    logging: Logging = Field(default_factory=Logging)

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
        """Load project secrets before committed configuration.

        Returns:
            The ordered settings sources.
        """

        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSource(settings_cls, paths.SECRETS_FILE, _SecretsFile),
            _YamlSource(settings_cls, paths.CONFIG_FILE, _ConfigFile),
        )

    @model_validator(mode="after")
    def validate_llm_authentication(self) -> "Settings":
        """Require API authentication.

        Returns:
            The validated settings.

        Raises:
            ValueError: Raised when a required API key is missing.
        """

        if self.llm.provider != "openai-subscription" and not self.llm.api_key:
            raise ValueError("llm.api_key is required unless llm.provider is openai-subscription")
        return self
