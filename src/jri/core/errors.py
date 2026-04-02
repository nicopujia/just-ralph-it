class JriError(RuntimeError):
    pass


class HaltRequested(JriError):
    pass
