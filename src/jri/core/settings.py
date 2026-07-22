from pathlib import Path
from typing import Literal

from dotenv import find_dotenv
from openai import OpenAI
from openai.types.shared import ReasoningEffort
from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from jri.core import paths
from jri.lib.providers import codex


class Settings(BaseSettings):
    """Application settings loaded from the CLI and environment."""

    cwd: Path = Field(description="Current working directory.", default_factory=Path.cwd)
    brave_search_api_key: str | None = Field(default=None, description="API key for Brave Search LLM Context API.")
    explorer_model: str = Field(description="Model ID for the Explorer agent.")
    explorer_reasoning_effort: ReasoningEffort = Field(default="low", description="Explorer model reasoning effort.")
    explorer_temperature: float = Field(default=0, ge=0, le=2, description="Explorer model sampling temperature.")
    force: bool = Field(description="Force re-creation of base directory.", default=False)
    llm_provider: str | None = Field(
        default=None,
        description=(
            "Set to openai-codex to use an existing Codex ChatGPT login, set to an OpenAI-compatible base URL "
            "to use that provider with llm_api_key, or leave unset to use OpenAI with llm_api_key."
        ),
    )
    llm_api_key: str | None = Field(
        default=None,
        validate_default=True,
        description=(
            "A valid API key for the configured LLM provider. Not required when llm_provider is openai-codex."
        ),
    )
    logging_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description=f"Minimum logging level for logs saved under {paths.LOGS_DIR}/."
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

    @field_validator("llm_api_key")
    @classmethod
    def validate_llm_authentication(cls, value: str | None, info: ValidationInfo) -> str | None:
        """Require an API key for providers other than ChatGPT Codex.

        Returns:
            The validated API key.

        Raises:
            ValueError: Raised when an API key is required.
        """

        if info.data.get("llm_provider") != "openai-codex" and not value:
            raise ValueError("llm_api_key is required unless llm_provider is openai-codex")
        return value

    @property
    def llm_client(self) -> OpenAI:
        """Build an LLM client from the configured provider settings."""

        if self.llm_provider == "openai-codex":
            return codex.Client()
        return OpenAI(base_url=self.llm_provider, api_key=self.llm_api_key)

    def validate_authentication(self) -> None:
        """Validate authentication for the configured provider."""

        if self.llm_provider == "openai-codex":
            codex.Auth().validate()
