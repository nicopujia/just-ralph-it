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
