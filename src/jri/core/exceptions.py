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


# The provider answered, and its answer was to refuse the request.
# Nothing about that answer is JRI's to report or the user's to wait
# out: the same request will be refused the same way until what JRI
# asks with changes.
class ProviderRefusalError(ModelError): ...


# The provider gave JRI no answer it could use, and may give one
# later: an address that could not be reached, or a fault the provider
# reported about itself.
class ProviderUnavailableError(ModelError): ...


class RunDetached(BaseException):
    # Not an `Error`, and not an `Exception` either: a window leaving
    # is not a turn ending, and every recovery JRI has is written
    # against a failure. Anything catching one of those would report a
    # run that is still going as a run that stopped.
    ...
