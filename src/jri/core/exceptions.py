class Error(RuntimeError):
    """Common base class for all JRI exceptions.

    Inherits `RuntimeError` so that every JRI failure keeps reaching
    the agent tool loop and the terminal UI, which both already
    recover from one.
    """


class PersistenceError(Error):
    """Persisted JRI data is unavailable or invalid."""


class RepositoryStateError(Error):
    """The repository cannot back a specification generation."""


class SpecsError(Error):
    """Generated specifications are unusable or unsafe to apply."""


class ModelError(Error):
    """The model did not produce a usable response."""
