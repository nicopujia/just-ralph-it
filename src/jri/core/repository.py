from collections.abc import Sequence
from typing import override

from jri.lib import git

CO_AUTHOR = "ralphpujia <ralph@pujia.ar>"


class Repository(git.Repository):
    # Every commit records who wrote it alongside the person Git
    # credits as the author, ahead of whatever else it has to say.
    @override
    def commit(self, message: str, trailers: Sequence[str] = (), *, paths: Sequence[str] = ()) -> str:
        return super().commit(message, (f"Co-authored-by: {CO_AUTHOR}", *trailers), paths=paths)
