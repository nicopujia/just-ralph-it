import os
from typing import ClassVar

from jri.lib import git


class Repository(git.Repository):
    # A private copy must keep its bytes and patch format under any global Git settings of the user.
    ISOLATED_SETTINGS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("apply.whitespace", "nowarn"),
        ("core.attributesFile", os.devnull),
        ("core.autocrlf", "false"),
        ("diff.context", "3"),
    )

    def isolate(self) -> None:
        for name, value in self.ISOLATED_SETTINGS:
            self._run("config", name, value)

    # Write the index as a tree and return its name. A tree needs no author, so this works with no Git identity.
    def write_tree(self) -> str:
        return os.fsdecode(self._run("write-tree").stdout).strip()

    # Add a rule that only this clone reads. Git keeps `info/exclude` out of every commit, so the project gets no file.
    def exclude(self, pattern: str) -> None:
        exclude_file = self._common_directory / "info" / "exclude"
        # A rule can name a path that is not UTF-8. Keep its bytes.
        content = exclude_file.read_bytes() if exclude_file.exists() else b""
        rule = os.fsencode(pattern)
        if rule in content.splitlines():
            return
        exclude_file.parent.mkdir(exist_ok=True)
        separator = b"" if not content or content.endswith(b"\n") else b"\n"
        exclude_file.write_bytes(content + separator + rule + b"\n")
