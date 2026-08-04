import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

from jri.lib import git

from . import paths
from .notes import Notebook
from .repository import Repository
from .settings import Settings


@dataclass(frozen=True)
class Workspace:
    """A project's JRI directory, as `create` found or left it."""

    PROJECT_IGNORES: ClassVar[tuple[str, ...]] = (".DS_Store", ".env", ".env.*")
    INITIAL_COMMIT_MESSAGE: ClassVar[str] = "jri: initialize project"

    directory: Path
    config_file: Path
    created: bool
    repository_created: bool

    @staticmethod
    def find_project(cwd: Path) -> Path:
        """Find the directory a project is rooted at.

        A project is the Git worktree holding a directory, since JRI
        stores the specifications it writes in commits, and the
        directory itself outside any worktree.

        Returns:
            The directory the project is rooted at.
        """

        return git.find_root(cwd) or cwd

    @classmethod
    def create(cls, cwd: Path, *, force: bool = False) -> Self:
        """Create a project's JRI workspace, keeping what exists.

        Projects outside a Git repository get one holding everything
        already there, since JRI stores the specifications it writes in
        commits and reads its baseline from the latest one. Forcing
        writes the configuration file again and throws away the
        conversation, the notes, the logs, and the generated
        specifications.

        Returns:
            The workspace found or created.
        """

        repository_created = git.find_root(cwd) is None
        repository = Repository.init(cwd)
        workspace = cwd / paths.WORKSPACE_DIR
        config_file = cwd / paths.CONFIG_FILE
        created = not config_file.exists()
        if force:
            for path in paths.RESET_PATHS:
                target = cwd / path
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
        workspace.mkdir(exist_ok=True, parents=True)
        if created or force:
            config_file.write_text(Settings.render_config(), encoding="utf-8")
        Notebook(cwd / paths.NOTEBOOK_FILE)
        (cwd / paths.LOGS_DIR).mkdir(exist_ok=True)

        ignored = [Path(path).name for path in (paths.SESSION_FILE, paths.LOGS_DIR, paths.VISUALIZATION_FILE)]
        gitignore = cwd / paths.GITIGNORE_FILE
        content = gitignore.read_text() if gitignore.exists() else ""
        missing = [name for name in ignored if name not in content.splitlines()]
        if missing:
            separator = "" if not content or content.endswith("\n") else "\n"
            gitignore.write_text(f"{content}{separator}{'\n'.join(missing)}\n")

        if repository_created:
            project_gitignore = cwd / paths.PROJECT_GITIGNORE_FILE
            if not project_gitignore.exists():
                project_gitignore.write_text(f"{'\n'.join(cls.PROJECT_IGNORES)}\n")
            repository.stage((".",))
            repository.commit(cls.INITIAL_COMMIT_MESSAGE)
        return cls(workspace, config_file, created, repository_created)
