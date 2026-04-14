class JriError(RuntimeError):
    pass


class HaltRequested(JriError):
    pass


class RecoveryError(JriError):
    pass


class RestartRequested(JriError):
    def __init__(self, *, remaining_tasks: int | None = None) -> None:
        super().__init__("restart requested")
        self.remaining_tasks = remaining_tasks
