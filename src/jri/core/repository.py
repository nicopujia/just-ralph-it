from typing import override

from jri.lib import git

CO_AUTHOR = "ralphpujia <ralph@pujia.ar>"


class Repository(git.Repository):
    """The project's repository, which Ralph commits into."""

    @override
    def commit(self, message: str, co_author: str = CO_AUTHOR) -> str:
        """Create a commit crediting Ralph alongside the author.

        Returns:
            The new commit ID.
        """

        return super().commit(message, co_author)
