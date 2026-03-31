"""Application configuration using Pydantic settings."""

import logging
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def _get_ralph_bot_github_token() -> str:
    """Read Ralph bot GitHub token from gh CLI."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Mode (DEV or PROD)
    mode: Literal["DEV", "PROD"] = "PROD"

    # Maintenance mode
    maintenance_mode: bool = False

    # Security
    secret_key: str = "dev"

    # GitHub OAuth (mode-prefixed)
    dev_github_client_id: str = ""
    dev_github_client_secret: str = ""
    prod_github_client_id: str = ""
    prod_github_client_secret: str = ""

    # Stripe (mode-prefixed)
    dev_stripe_secret_key: str = ""
    prod_stripe_secret_key: str = ""

    # Base URL (mode-prefixed)
    dev_base_url: str = "http://localhost:8000"
    prod_base_url: str = "http://localhost:8000"

    # Pricing (in cents)
    price_per_task: int = 400  # $4 per task
    max_free_projects: int = 3
    price_pro_monthly: int = 2000  # $20/mo

    # Agent models
    ralph_model: str = "openai/gpt-5.4-mini"
    ralphy_model: str = "openai/gpt-5.4"

    @field_validator("maintenance_mode", mode="before")
    @classmethod
    def parse_maintenance_mode(cls, v):
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.lower() in ("1", "true", "yes")
        return False

    @model_validator(mode="after")
    def validate_settings(self):
        # Warn about insecure secret key in PROD
        if self.secret_key == "dev" and self.mode == "PROD":
            logger.warning(
                "SECRET_KEY not set - using insecure default in PROD mode"
            )

        # Validate required GitHub credentials
        if not self.github_client_id or not self.github_client_secret:
            raise ValueError(
                f"Missing required environment variables: "
                f"{self.mode}_GITHUB_CLIENT_ID, {self.mode}_GITHUB_CLIENT_SECRET"
            )

        # Warn about missing Stripe key
        if not self.stripe_secret_key:
            logger.warning(
                "STRIPE_SECRET_KEY not set - Stripe payments will not work"
            )

        return self

    @property
    def github_client_id(self) -> str:
        """Get GitHub client ID for current mode."""
        if self.mode == "DEV":
            return self.dev_github_client_id
        return self.prod_github_client_id

    @property
    def github_client_secret(self) -> str:
        """Get GitHub client secret for current mode."""
        if self.mode == "DEV":
            return self.dev_github_client_secret
        return self.prod_github_client_secret

    @property
    def stripe_secret_key(self) -> str:
        """Get Stripe secret key for current mode."""
        if self.mode == "DEV":
            return self.dev_stripe_secret_key
        return self.prod_stripe_secret_key

    @property
    def base_url(self) -> str:
        """Get base URL for current mode."""
        if self.mode == "DEV":
            return self.dev_base_url
        return self.prod_base_url


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Singleton instance for backwards compatibility
_settings = get_settings()

# Data directory
DATA_DIR: Path = Path(__file__).resolve().parent.parent.parent / ".data"

# Ralph bot token (loaded once at import)
RALPH_BOT_GITHUB_TOKEN: str = _get_ralph_bot_github_token()

# Export individual settings for backwards compatibility
MODE = _settings.mode
MAINTENANCE_MODE = _settings.maintenance_mode
SECRET_KEY = _settings.secret_key
GITHUB_CLIENT_ID = _settings.github_client_id
GITHUB_CLIENT_SECRET = _settings.github_client_secret
STRIPE_SECRET_KEY = _settings.stripe_secret_key
BASE_URL = _settings.base_url
PRICE_PER_TASK = _settings.price_per_task
MAX_FREE_PROJECTS = _settings.max_free_projects
PRICE_PRO_MONTHLY = _settings.price_pro_monthly
RALPH_MODEL = _settings.ralph_model
RALPHY_MODEL = _settings.ralphy_model
