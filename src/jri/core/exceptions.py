from dataclasses import dataclass

from pydantic import ValidationError


class Error(Exception):
    """Common base class for all JRI exceptions."""


class AuthError(Error):
    """LLM provider authentication is unavailable or invalid."""


@dataclass(frozen=True)
class ConfigurationError(Error):
    """Application configuration is invalid."""

    validation_error: ValidationError
