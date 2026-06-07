"""Explorer test doubles."""

from pathlib import Path


class RecordingExplorer:
    """Explorer double that records requests and returns fixed output."""

    def __init__(self, output: str = "Summary:\n- inspected") -> None:
        self.output: str = output
        self.requests: list[tuple[Path, str]] = []

    async def run(self, *, project_root: Path, request: str) -> str:
        """Record the request and return compact fake context."""
        self.requests.append((project_root, request))
        return self.output


class FailingExplorer:
    """Explorer double that raises like a failing provider call."""

    async def run(self, *, project_root: Path, request: str) -> str:
        """Fail after accepting the exploration request."""
        _ = (project_root, request)
        msg = "status_code: 404, body: {'message': 'No endpoints found'}"
        raise RuntimeError(msg)
