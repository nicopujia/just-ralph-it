import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from jri.lib import git, prompt

from . import paths
from .exceptions import RepositoryStateError, SpecsError
from .repository import Repository
from .workspace import Workspace

# What the commit that accepted a generation calls itself, so Git can
# answer which commit that was.
ACCEPTANCE_TRAILER = "JRI-Specifications: accepted"

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

    def prepare(self) -> Baseline:
        notebook = self.workspace.notebook_file.read_bytes()
        self._check_state()
        if not self.repository.has_commit():
            return Baseline(None, notebook, b"", {}, {})
        commit = self.repository.read_head()
        specs = self.repository.read_tree(commit, paths.SPECS_DIR)
        accepted = self.repository.find_commit(ACCEPTANCE_TRAILER)
        if accepted is None:
            if specs:
                raise RepositoryStateError("Git holds specifications JRI did not write. Remove them before Ralphing.")
            return Baseline(commit, notebook, b"", {}, {})
        functional = self.repository.read_tree(accepted, paths.FUNCTIONAL_SPECS_DIR)
        architecture = self.repository.read_tree(accepted, paths.ARCHITECTURE_SPECS_DIR)
        if specs != functional | architecture:
            raise RepositoryStateError("Checked-out specifications differ from the ones JRI accepted.")
        logger.info("baseline_prepared head=%s accepted=%s functional=%d", commit, accepted, len(functional))
        return Baseline(
            commit, notebook, self.repository.read_file(accepted, paths.NOTEBOOK_FILE), functional, architecture
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
        # The path is JRI's own: an rglob over the specification tree
        # named it, under a root `_validate_patch` bounds. So it stays
        # prose, while the body a model wrote is quoted.
        return (
            "\n\n".join(
                f"File: {path.removeprefix(prefix)}\n{prompt.render(content=content.decode())}"
                for path, content in sorted(files.items())
            )
            or "(empty)"
        )

    def accept(self, patch: bytes, baseline: Baseline) -> str:
        # A commit the user makes mid-run moves HEAD without touching
        # what this run is about, so what has to have held still is the
        # specification tree the patch was written against.
        head_specs = (
            self.repository.read_tree(self.repository.read_head(), paths.SPECS_DIR)
            if self.repository.has_commit()
            else {}
        )
        if head_specs != baseline.functional | baseline.architecture:
            raise RepositoryStateError("The specifications changed during generation. Try again.")
        if self.workspace.notebook_file.read_bytes() != baseline.notebook:
            raise RepositoryStateError("The project notes changed during generation. Try again.")
        self._check_state()
        self.repository.apply_patch(patch)
        # The intent alone, so JRI never writes over content the user
        # staged for a path of its own, and a crash before the commit
        # leaves nothing behind for their next commit to pick up.
        try:
            self.repository.stage(paths.COMMITTED_PATHS, intent_to_add=True)
            commit = self.repository.commit(
                "jri: update specifications", trailers=(ACCEPTANCE_TRAILER,), paths=paths.COMMITTED_PATHS
            )
        except git.Error:
            self.repository.unstage(paths.COMMITTED_PATHS)
            self.repository.apply_patch(patch, reverse=True)
            raise
        logger.info("specs_committed commit=%s", commit)
        return commit

    def _check_state(self) -> None:
        # Off a branch, Git takes the commit and leaves it reachable
        # only from where HEAD stands, so going back to the branch
        # loses it: every stopped rebase, every bisect, and every
        # checkout of a commit or a tag sits here. The refs a rebase
        # writes cannot stand in for this, since one of them outlives
        # the rebase and a rebase held at a break writes none at all.
        if not self.repository.is_on_branch():
            raise RepositoryStateError(
                "Git is not on a branch, so JRI's commit would be lost. Check out a branch before Ralphing."
            )
        # A merge and a cherry-pick both keep the branch, and Git
        # refuses a partial commit under either. A merge says so with
        # MERGE_HEAD even when it merged cleanly, and a cherry-pick
        # only ever stops with the conflict that stopped it.
        if self.repository.has_conflicts() or self.repository.has_commit("MERGE_HEAD"):
            raise RepositoryStateError("Finish the merge or cherry-pick in progress before Ralphing.")
        blockers = sorted(entry.path for entry in self.repository.read_status((paths.SPECS_DIR,)))
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
