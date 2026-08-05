class Error(RuntimeError):
    # Inheriting `RuntimeError` keeps every JRI failure reaching the
    # agent tool loop and the terminal UI, which both already recover
    # from one.
    ...


class PersistenceError(Error): ...


class RepositoryStateError(Error): ...


class SpecsError(Error): ...


class ModelError(Error): ...


class UsageLimitError(ModelError): ...
