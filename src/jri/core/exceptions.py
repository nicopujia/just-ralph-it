class Error(Exception):
    """Common base class for all JRI exceptions."""


class PersistenceError(Error):
    """Persisted JRI data is unavailable or invalid."""
