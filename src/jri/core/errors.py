from __future__ import annotations


class JriError(RuntimeError):
    pass


class HaltRequested(JriError):
    pass
