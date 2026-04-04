class JriError(RuntimeError):
    pass


class HaltRequested(JriError):
    pass


class RecoveryError(JriError):
    pass
