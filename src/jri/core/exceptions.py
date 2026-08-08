class Error(RuntimeError):
    # Inheriting `RuntimeError` keeps every JRI failure reaching the
    # agent tool loop and the terminal UI, which both already recover
    # from one.
    ...


class PersistenceError(Error): ...


class ReplayError(Error): ...


class RepositoryStateError(Error): ...


class SpecsError(Error): ...


class ModelError(Error): ...


class UsageLimitError(ModelError): ...


class RunDetached(BaseException):
    # Not an `Error`, and not an `Exception` either: a window leaving
    # is not a turn ending, and every recovery JRI has is written
    # against a failure. Anything catching one of those would report a
    # run that is still going as a run that stopped.
    ...
