from collections.abc import Sequence
from typing import override

from jri.lib import git

CO_AUTHOR = "ralphpujia <ralph@pujia.ar>"
# This trailer identifies the commit that accepted a generation. Git can then find that commit.
ACCEPTANCE_TRAILER = "JRI-Specifications: accepted"


class Repository(git.Repository):
    # Add the JRI author trailer before all other trailers. Git also records the user as the commit author.
    @override
    def commit(self, message: str, trailers: Sequence[str] = (), *, paths: Sequence[str] = ()) -> str:
        return super().commit(message, (f"Co-authored-by: {CO_AUTHOR}", *trailers), paths=paths)

    # Add a rule that only this clone reads. Git keeps `info/exclude` out of every commit, so the project gets no file.
    def exclude(self, pattern: str) -> None:
        exclude_file = self._common_directory / "info" / "exclude"
        content = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
        if pattern in content.splitlines():
            return
        exclude_file.parent.mkdir(exist_ok=True)
        separator = "" if not content or content.endswith("\n") else "\n"
        exclude_file.write_text(f"{content}{separator}{pattern}\n", encoding="utf-8", newline="\n")
