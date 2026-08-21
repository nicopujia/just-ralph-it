class Error(RuntimeError):
    # Inherit `RuntimeError` so JRI failures reach the agent tool loop and terminal UI, which handle this error.
    ...


class PersistenceError(Error): ...


class ReplayError(Error): ...


class RepositoryStateError(Error): ...


class SpecsError(Error): ...


class ModelError(Error): ...


class UsageLimitError(ModelError): ...


# The provider refused the request. It refuses the same request again, until JRI changes that request.
class ProviderRefusalError(ModelError): ...


# The provider gave no usable answer, but it can answer later.
# This includes an address that JRI cannot reach, and a fault at the provider.
class ProviderUnavailableError(ModelError): ...


class RunDetached(BaseException):
    # This is not an `Error` or an `Exception`. A window that closes does not end a turn.
    # JRI would report a live run as stopped if it read this as a failure.
    ...


# The run concluded that it stopped. A stop can begin in the window that watches the run, or in another process
# that asked for it. Only the run records that it did stop.
class RunStopped(BaseException):
    # This is not an `Error` or an `Exception`. A stop that the user asked for is not a failure.
    # JRI would roll the turn back and report a crash if it read this as a failure.
    ...
