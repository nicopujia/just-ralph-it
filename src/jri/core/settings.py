from pathlib import Path
from typing import Literal

from dotenv import find_dotenv
from openai import OpenAI
from openai.types.shared import ReasoningEffort
from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from .exceptions import ConfigurationError


class Settings(BaseSettings):
    """Application settings loaded from the CLI and environment."""

    cwd: Path = Field(description="Current working directory.", default_factory=Path.cwd)
    brave_api_key: str | None = Field(default=None, description="API key for Brave Search LLM Context API.")
    explorer_model: str = Field(description="Model ID for the Explorer agent.")
    explorer_reasoning_effort: ReasoningEffort = Field(default="low", description="Explorer model reasoning effort.")
    explorer_temperature: float = Field(default=0, ge=0, le=2, description="Explorer model sampling temperature.")
    force: bool = Field(description="Force re-creation of base directory.", default=False)
    llm_api_key: str = Field(
        description=(
            "A valid API key for the LLM provider, which is defined by JRI_LLM_PROVIDER_BASE_URL. "
            "Default provider is OpenAI."
        )
    )
    llm_base_url: str | None = Field(
        default=None, description=("Any OpenAI-compatible provider base URL. Defaults to OpenAI as the provider.")
    )
    logging_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Minimum logging level for logs saved under .jri/logs/."
    )
    interviewer_model: str = Field(description="Model ID for the Interviewer agent.")
    interviewer_reasoning_effort: ReasoningEffort = Field(
        default="high", description="Interviewer model reasoning effort."
    )
    interviewer_temperature: float = Field(
        default=0.7, ge=0, le=2, description="Interviewer model sampling temperature."
    )

    model_config = SettingsConfigDict(
        cli_kebab_case=True,
        cli_parse_args=True,
        cli_implicit_flags="toggle",
        env_prefix="JRI_",
        env_file=find_dotenv(usecwd=True),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def llm_client(self) -> OpenAI:
        """Build an LLM client from the configured provider settings."""

        return OpenAI(base_url=self.llm_base_url, api_key=self.llm_api_key)


def get_settings() -> Settings:
    """Return validated application settings.

    Returns:
        The validated application settings.

    Raises:
        ConfigurationError: Raised when the settings are invalid.
    """

    try:
        return Settings()  # pyright: ignore[reportCallIssue]
    except ValidationError as error:
        raise ConfigurationError(error) from error
