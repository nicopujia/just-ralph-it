import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from jri.lib import git

from . import paths
from .notes import Notebook
from .repository import Repository


@dataclass(frozen=True)
class Workspace:
    PROJECT_IGNORES: ClassVar[tuple[str, ...]] = (".DS_Store", ".env", ".env.*")

    root: Path

    @staticmethod
    def find() -> "Workspace":
        cwd = Path.cwd()
        return Workspace(git.find_root(cwd) or cwd)

    @property
    def directory(self) -> Path:
        return self.root / paths.WORKSPACE_DIR

    @property
    def config_file(self) -> Path:
        return self.root / paths.CONFIG_FILE

    @property
    def gitignore_file(self) -> Path:
        return self.root / paths.GITIGNORE_FILE

    @property
    def project_gitignore_file(self) -> Path:
        return self.root / paths.PROJECT_GITIGNORE_FILE

    @property
    def notebook_file(self) -> Path:
        return self.root / paths.NOTEBOOK_FILE

    @property
    def session_file(self) -> Path:
        return self.root / paths.SESSION_FILE

    @property
    def visualization_file(self) -> Path:
        return self.root / paths.VISUALIZATION_FILE

    @property
    def logs_dir(self) -> Path:
        return self.root / paths.LOGS_DIR

    @property
    def reset_paths(self) -> tuple[Path, ...]:
        return tuple(self.root / path for path in paths.RESET_PATHS)

    # The rendered configuration comes in rather than being read from
    # `Settings`, so locating a workspace never depends on loading one.
    def install(self, config: str, *, force: bool = False) -> "Installation":
        repository_created = git.find_root(self.root) is None
        Repository.init(self.root)
        created = not self.config_file.exists()
        if force:
            for path in self.reset_paths:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
        self.directory.mkdir(exist_ok=True, parents=True)
        if created or force:
            self.config_file.write_text(config, encoding="utf-8")
        Notebook(self.notebook_file)
        self.logs_dir.mkdir(exist_ok=True)

        ignored = [path.name for path in (self.session_file, self.logs_dir, self.visualization_file)]
        content = self.gitignore_file.read_text() if self.gitignore_file.exists() else ""
        missing = [name for name in ignored if name not in content.splitlines()]
        if missing:
            separator = "" if not content or content.endswith("\n") else "\n"
            self.gitignore_file.write_text(f"{content}{separator}{'\n'.join(missing)}\n")

        # The ignore file a project brought along is not JRI's to
        # rewrite, so only a repository JRI creates gets one, and what
        # it holds is what keeps those patterns out of the first commit
        # the user makes.
        if repository_created and not self.project_gitignore_file.exists():
            self.project_gitignore_file.write_text(f"{'\n'.join(self.PROJECT_IGNORES)}\n")
        return Installation(self, created=created, repository_created=repository_created)


@dataclass(frozen=True)
class Installation:
    workspace: Workspace
    created: bool
    repository_created: bool
