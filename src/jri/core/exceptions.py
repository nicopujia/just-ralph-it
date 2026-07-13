class Error(Exception):
    """Common base class for all JRI exceptions."""


class AuthError(Error):
    """LLM provider authentication is unavailable or invalid."""
