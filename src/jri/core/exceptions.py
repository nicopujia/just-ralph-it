from pydantic import ValidationError


class JriError(Exception):
    """Common base class for all JRI exceptions."""


class JriConfigurationError(JriError):
    """Application configuration is invalid."""

    def __init__(self, validation_error: ValidationError) -> None:
        super().__init__()
        self.validation_error = validation_error
