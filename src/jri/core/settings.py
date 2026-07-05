from typing import ClassVar

from openai import OpenAI
from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from .exceptions import ConfigurationError


class Settings(BaseSettings):
    llm_api_key: str = Field(
        description=(
            "A valid API key for the LLM provider, which is defined by "
            "JRI_LLM_PROVIDER_BASE_URL. Default provider is OpenAI."
        ),
    )
    llm_provider_base_url: str | None = Field(
        default=None,
        description=(
            "Any OpenAI-compatible provider base URL. "
            "Defaults to OpenAI as the provider."
        ),
    )
    interviewer_model: str = Field(
        description="Model ID for the Interviewer agent.",
    )
    explorer_model: str = Field(
        description="Model ID for the Explorer agent.",
    )
    brave_api_key: str | None = Field(
        default=None,
        description="API key for Brave Search LLM Context API.",
    )

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def llm_client(self) -> OpenAI:
        return OpenAI(
            base_url=self.llm_provider_base_url,
            api_key=self.llm_api_key,
        )


def get_settings() -> Settings:
    try:
        return Settings()  # pyright: ignore[reportCallIssue]
    except ValidationError as error:
        raise ConfigurationError(error) from error
