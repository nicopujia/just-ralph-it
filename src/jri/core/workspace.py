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
    def log_file(self) -> Path:
        return self.root / paths.LOG_FILE

    @property
    def generation_dir(self) -> Path:
        return self.root / paths.GENERATION_DIR

    @property
    def acceptance_file(self) -> Path:
        return self.root / paths.ACCEPTANCE_FILE

    @property
    def reset_paths(self) -> tuple[Path, ...]:
        return tuple(self.root / path for path in paths.RESET_PATHS)

    # What a run writes down while it works, and never what it commits.
    def open_generation_dir(self) -> Path:
        self.generation_dir.mkdir(exist_ok=True, parents=True)
        # Rooted at the workspace and closed by a slash, so the rule
        # answers for this directory and for no `generation` a
        # specification tree happens to hold.
        self._ignore(f"/{self.generation_dir.name}/")
        return self.generation_dir

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
            self.config_file.write_text(config, encoding="utf-8", newline="\n")
        Notebook(self.notebook_file)
        self.logs_dir.mkdir(exist_ok=True)

        self._ignore(*(path.name for path in (self.session_file, self.logs_dir, self.visualization_file)))

        # The ignore file a project brought along is not JRI's to
        # rewrite, so only a repository JRI creates gets one, and what
        # it holds is what keeps those patterns out of the first commit
        # the user makes.
        if repository_created and not self.project_gitignore_file.exists():
            self.project_gitignore_file.write_text(
                f"{'\n'.join(self.PROJECT_IGNORES)}\n", encoding="utf-8", newline="\n"
            )
        return Installation(self, created=created, repository_created=repository_created)

    # Read back and topped up on every call, since a rule checked for
    # its existence alone is one nothing puts back once something has
    # replaced it. The file is the one JRI commits, so Git holds what
    # it says and reports a line going missing -- where a rule sitting
    # in the directory it hides ignores itself, and takes its own
    # absence with it.
    def _ignore(self, *patterns: str) -> None:
        content = self.gitignore_file.read_text(encoding="utf-8") if self.gitignore_file.exists() else ""
        missing = [pattern for pattern in patterns if pattern not in content.splitlines()]
        if not missing:
            return
        separator = "" if not content or content.endswith("\n") else "\n"
        self.gitignore_file.write_text(f"{content}{separator}{'\n'.join(missing)}\n", encoding="utf-8", newline="\n")


@dataclass(frozen=True)
class Installation:
    workspace: Workspace
    created: bool
    repository_created: bool
