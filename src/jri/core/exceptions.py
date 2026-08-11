class Error(RuntimeError):
    # Inherit `RuntimeError` so JRI failures reach the agent tool loop and terminal UI, which handle this error.
    ...


class PersistenceError(Error): ...


class ReplayError(Error): ...


class RepositoryStateError(Error): ...


class SpecsError(Error): ...


class ModelError(Error): ...


class UsageLimitError(ModelError): ...


# The provider refused the request. Retrying the same request gives the same refusal until JRI changes its request.
class ProviderRefusalError(ModelError): ...


# The provider gave no usable answer but can answer later. This includes an unreachable address and a provider fault.
class ProviderUnavailableError(ModelError): ...


class RunDetached(BaseException):
    # This is not an `Error` or an `Exception`. A closing window does not end a turn.
    # Treating it as a failure would report a live run as stopped.
    ...
