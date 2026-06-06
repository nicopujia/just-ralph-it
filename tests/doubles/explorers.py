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
