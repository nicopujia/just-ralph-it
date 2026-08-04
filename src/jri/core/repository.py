from typing import override

from jri.lib import git

CO_AUTHOR = "ralphpujia <ralph@pujia.ar>"


class Repository(git.Repository):
    # Every commit records who wrote it alongside the person Git
    # credits as the author, so the default stands in for no co-author.
    @override
    def commit(self, message: str, co_author: str | None = CO_AUTHOR) -> str:
        return super().commit(message, co_author)
