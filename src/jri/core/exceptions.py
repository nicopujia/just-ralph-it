class JriError(Exception):
    """Common base class for all JRI exceptions."""


class JriUnauthenticatedError(JriError):
    """No valid API key available in environment."""
