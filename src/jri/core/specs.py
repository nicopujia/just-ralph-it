import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from jri.lib import git

from . import constants, paths

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Baseline:
    """Repository state used to generate the next specifications."""

    commit: str | None
    notebook: bytes
    accepted_notebook: bytes
    functional: dict[str, bytes]
    architecture: dict[str, bytes]


class Specs:
    """Manage the Git-backed specification lifecycle."""

    def __init__(self, path: Path) -> None:
        self.repository = git.Repository(path)

    def prepare(self, active_commit: str | None) -> Baseline:
        """Load and validate the baseline for specification generation.

        Returns:
            The current repository and accepted specification state.

        Raises:
            RuntimeError: If the repository state cannot produce a valid
                specification baseline.
        """

        notebook = (self.repository.path / paths.NOTEBOOK_FILE).read_bytes()
        commit = self.repository.read_head() if self.repository.has_commit() else None
        self._check_status(commit)
        if active_commit is None:
            if commit is not None and self.repository.read_tree(commit, paths.SPECS_DIR):
                raise RuntimeError("Existing specifications have no active JRI commit.")
            return Baseline(commit, notebook, b"", {}, {})
        if commit is None or not self.repository.has_commit(active_commit):
            raise RuntimeError("The active specification commit is missing from Git.")
        if not self.repository.is_ancestor(active_commit, commit):
            raise RuntimeError("The active specification commit is not reachable from HEAD.")
        functional = self.repository.read_tree(active_commit, paths.FUNCTIONAL_SPECS_DIR)
        architecture = self.repository.read_tree(active_commit, paths.ARCHITECTURE_SPECS_DIR)
        if (
            self.repository.read_tree(commit, paths.FUNCTIONAL_SPECS_DIR) != functional
            or self.repository.read_tree(commit, paths.ARCHITECTURE_SPECS_DIR) != architecture
        ):
            raise RuntimeError("Checked-out specifications differ from the active JRI commit.")
        logger.info("baseline_prepared head=%s active=%s functional=%d", commit, active_commit, len(functional))
        return Baseline(
            commit, notebook, self.repository.read_file(active_commit, paths.NOTEBOOK_FILE), functional, architecture
        )

    def apply(self, repository: git.Repository, patch: str, root: str) -> None:
        """Validate a model patch and re-root it into the repository.

        Models patch neutral roots such as ``functional``, so the
        patch is validated against ``root`` and applied below the
        specifications directory.

        Raises:
            git.Error: If Git refuses the patch.
        """

        self._validate_patch(patch, root)
        try:
            repository.apply_patch(patch.encode(), index=True, directory=paths.SPECS_DIR)
        except git.Error:
            # The patch is the only evidence of why generation failed.
            logger.exception("patch_rejected root=%s patch=%r", root, patch)
            raise
        logger.info("patch_applied root=%s characters=%d", root, len(patch))

    @staticmethod
    def read(worktree: Path, root: str) -> dict[str, bytes]:
        """Read every Markdown specification below a worktree path.

        Returns:
            Repository-relative paths mapped to their contents.
        """

        directory = worktree / root
        return {path.relative_to(worktree).as_posix(): path.read_bytes() for path in sorted(directory.rglob("*.md"))}

    @staticmethod
    def render(files: dict[str, bytes]) -> str:
        """Render a specification tree as model context.

        Returns:
            The contents of every specification file, keyed by its path
            below the specifications directory.
        """

        prefix = f"{paths.SPECS_DIR}/"
        return (
            "\n\n".join(
                f"File: {path.removeprefix(prefix)}\n\n{content.decode()}" for path, content in sorted(files.items())
            )
            or "(empty)"
        )

    def accept(self, patch: bytes, baseline: Baseline) -> str:
        """Commit a generated specification bundle.

        Returns:
            The accepted commit ID.

        Raises:
            RuntimeError: If the repository changed during generation.
        """

        head = self.repository.read_head() if self.repository.has_commit() else None
        if head != baseline.commit or (self.repository.path / paths.NOTEBOOK_FILE).read_bytes() != baseline.notebook:
            raise RuntimeError("The project changed while specifications were being generated. Try again.")
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
        commit = self.repository.commit("jri: update specifications", constants.CO_AUTHOR)
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
            raise RuntimeError(
                "Commit or remove these files before Ralphing:\n" + "\n".join(f"- {path}" for path in blockers)
            )

    @staticmethod
    def _validate_patch(patch: str, root: str) -> None:
        if "GIT binary patch" in patch or "Binary files " in patch:
            raise RuntimeError("Specification patches cannot contain binary files.")
        patch_paths: list[str] = []
        for line in patch.splitlines():
            if (
                line.startswith(("old mode ", "new mode "))
                or (line.startswith(("new file mode ", "deleted file mode ")) and not line.endswith(" 100644"))
                or " 120000" in line
            ):
                raise RuntimeError("Specification patches cannot change file modes or symlinks.")
            if line.startswith("diff --git "):
                match line.split():
                    case ["diff", "--git", old, new] if old.startswith("a/") and new.startswith("b/"):
                        patch_paths.extend((old[2:], new[2:]))
                    case _:
                        raise RuntimeError("Malformed specification patch path.")
            elif line.startswith(("--- ", "+++ ")):
                raw_path = line[4:].split("\t", maxsplit=1)[0]
                if raw_path != "/dev/null":
                    if not raw_path.startswith(("a/", "b/")):
                        raise RuntimeError("Malformed specification patch path.")
                    patch_paths.append(raw_path[2:])
            elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
                patch_paths.append(line.split(" ", maxsplit=2)[2])
        if not patch_paths:
            raise RuntimeError("Specification patch must change at least one file.")
        for raw_path in patch_paths:
            path = PurePosixPath(raw_path)
            if path.is_absolute() or ".." in path.parts or path.suffix != ".md" or not path.is_relative_to(root):
                raise RuntimeError(f"Specification patch cannot change `{raw_path}`.")
