import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from jri.lib import git

from . import paths
from .exceptions import RepositoryStateError, SpecsError
from .repository import Repository
from .workspace import Workspace

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Baseline:
    commit: str | None
    notebook: bytes
    accepted_notebook: bytes
    functional: dict[str, bytes]
    architecture: dict[str, bytes]


class Specs:
    def __init__(self, path: Path) -> None:
        self.repository = Repository(path)
        self.workspace = Workspace(self.repository.path)

    def prepare(self, active_commit: str | None) -> Baseline:
        notebook = self.workspace.notebook_file.read_bytes()
        commit = self.repository.read_head() if self.repository.has_commit() else None
        self._check_status(commit)
        if active_commit is None:
            if commit is not None and self.repository.read_tree(commit, paths.SPECS_DIR):
                raise RepositoryStateError("Existing specifications have no active JRI commit.")
            return Baseline(commit, notebook, b"", {}, {})
        if commit is None or not self.repository.has_commit(active_commit):
            raise RepositoryStateError("The active specification commit is missing from Git.")
        if not self.repository.is_ancestor(active_commit, commit):
            raise RepositoryStateError("The active specification commit is not reachable from HEAD.")
        functional = self.repository.read_tree(active_commit, paths.FUNCTIONAL_SPECS_DIR)
        architecture = self.repository.read_tree(active_commit, paths.ARCHITECTURE_SPECS_DIR)
        if (
            self.repository.read_tree(commit, paths.FUNCTIONAL_SPECS_DIR) != functional
            or self.repository.read_tree(commit, paths.ARCHITECTURE_SPECS_DIR) != architecture
        ):
            raise RepositoryStateError("Checked-out specifications differ from the active JRI commit.")
        logger.info("baseline_prepared head=%s active=%s functional=%d", commit, active_commit, len(functional))
        return Baseline(
            commit, notebook, self.repository.read_file(active_commit, paths.NOTEBOOK_FILE), functional, architecture
        )

    def apply(self, repository: git.Repository, patch: str, model_root: str) -> None:
        self._validate_patch(patch, model_root)
        try:
            repository.apply_patch(patch.encode(), index=True, directory=paths.SPECS_DIR)
        except git.Error:
            # The patch is the only evidence of why generation failed.
            logger.exception("patch_rejected root=%s patch=%r", model_root, patch)
            raise
        logger.info("patch_applied root=%s characters=%d", model_root, len(patch))

    @staticmethod
    def read(worktree: Path, directory: str) -> dict[str, bytes]:
        root = worktree / directory
        return {path.relative_to(worktree).as_posix(): path.read_bytes() for path in sorted(root.rglob("*.md"))}

    @staticmethod
    def render(files: dict[str, bytes]) -> str:
        prefix = f"{paths.SPECS_DIR}/"
        return (
            "\n\n".join(
                f"File: {path.removeprefix(prefix)}\n\n{content.decode()}" for path, content in sorted(files.items())
            )
            or "(empty)"
        )

    def accept(self, patch: bytes, baseline: Baseline) -> str:
        head = self.repository.read_head() if self.repository.has_commit() else None
        if head != baseline.commit or self.workspace.notebook_file.read_bytes() != baseline.notebook:
            raise RepositoryStateError("The project changed while specifications were being generated. Try again.")
        self._check_status(baseline.commit)
        self.repository.apply_patch(patch)
        self.repository.stage(
            (".",)
            if baseline.commit is None
            else (
                paths.CONFIG_FILE,
                paths.GITIGNORE_FILE,
                paths.NOTEBOOK_FILE,
                paths.FUNCTIONAL_SPECS_DIR,
                paths.ARCHITECTURE_SPECS_DIR,
            )
        )
        commit = self.repository.commit("jri: update specifications")
        logger.info("specs_committed commit=%s", commit)
        return commit

    def _check_status(self, baseline: str | None) -> None:
        if baseline is None:
            return
        blockers = sorted({
            path
            for entry in self.repository.read_status()
            for path in (entry.path, entry.original_path)
            if path is not None and path not in {paths.CONFIG_FILE, paths.GITIGNORE_FILE, paths.NOTEBOOK_FILE}
        })
        if blockers:
            raise RepositoryStateError(
                "Commit or remove these files before Ralphing:\n" + "\n".join(f"- {path}" for path in blockers)
            )

    @staticmethod
    def _validate_patch(patch: str, model_root: str) -> None:
        patch_paths: list[str] = []
        in_hunk = False
        for line in patch.splitlines():
            # Only the metadata of a patch says what it changes, since
            # a hunk body is prose that may read exactly like it. Every
            # body line carries a prefix, which the metadata never has.
            if in_hunk and (not line or line.startswith((" ", "+", "-", "\\"))):
                continue
            in_hunk = line.startswith("@@ ")
            if in_hunk:
                continue
            if line == "GIT binary patch" or line.startswith("Binary files "):
                raise SpecsError("Specification patches cannot contain binary files.")
            if (
                line.startswith(("old mode ", "new mode "))
                or (line.startswith(("new file mode ", "deleted file mode ")) and not line.endswith(" 100644"))
                or (line.startswith("index ") and line.endswith(" 120000"))
            ):
                raise SpecsError("Specification patches cannot change file modes or symlinks.")
            if line.startswith("diff --git "):
                # A file name may hold spaces, and Git leaves it
                # unquoted, so the halves are told apart by the
                # prefixes bounding them rather than by splitting.
                header = line.removeprefix("diff --git ")
                middle = header.find(" b/")
                if not header.startswith("a/") or middle < 0:
                    raise SpecsError("Malformed specification patch path.")
                patch_paths.extend((header[2:middle], header[middle + len(" b/") :]))
            elif line.startswith(("--- ", "+++ ")):
                raw_path = line[4:].split("\t", maxsplit=1)[0]
                if raw_path != "/dev/null":
                    if not raw_path.startswith(("a/", "b/")):
                        raise SpecsError("Malformed specification patch path.")
                    patch_paths.append(raw_path[2:])
            elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
                patch_paths.append(line.split(" ", maxsplit=2)[2])
        if not patch_paths:
            raise SpecsError("Specification patch must change at least one file.")
        for raw_path in patch_paths:
            path = PurePosixPath(raw_path)
            if path.is_absolute() or ".." in path.parts or path.suffix != ".md" or not path.is_relative_to(model_root):
                raise SpecsError(f"Specification patch cannot change `{raw_path}`.")
