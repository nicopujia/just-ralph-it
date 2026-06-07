from typing import ClassVar

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    llm_api_key: str = Field(
        description=(
            "A valid API key compatible with JRI_LLM_PROVIDER_BASE_URL. "
            "Default provider is OpenAI, so if you don't define base URL "
            "this should be OPENAI_API_KEY."
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

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="JRI_",
    )


def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]
