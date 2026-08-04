from typing import override

from jri.lib import git

CO_AUTHOR = "ralphpujia <ralph@pujia.ar>"


class Repository(git.Repository):
    @override
    def commit(self, message: str, co_author: str = CO_AUTHOR) -> str:
        return super().commit(message, co_author)
