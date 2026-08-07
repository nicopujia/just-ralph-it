import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict

from jri.lib import files, git, prompt

from . import paths
from .exceptions import PersistenceError, RepositoryStateError, SpecsError
from .repository import Repository
from .workspace import Workspace

# What the commit that accepted a generation calls itself, so Git can
# answer which commit that was.
ACCEPTANCE_TRAILER = "JRI-Specifications: accepted"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Baseline:
    commit: str | None
    accepted: str | None
    notebook: bytes
    accepted_notebook: bytes
    functional: dict[str, bytes]
    architecture: dict[str, bytes]


# An acceptance under way, written down before it touches the project
# so that undoing it never depends on reading the worktree back: the
# patch is what was applied, `accepted` is the acceptance commit the
# project held before it, and `indexed` the paths Git already tracked.
class Acceptance(BaseModel):
    accepted: str | None
    patch: str
    indexed: tuple[str, ...]

    model_config = ConfigDict(extra="forbid")


class Specs:
    def __init__(self, path: Path) -> None:
        self.repository = Repository(path)
        self.workspace = Workspace(self.repository.path)

    def prepare(self) -> Baseline:
        notebook = self._read_notebook()
        self._reconcile()
        self._check_state()
        if not self.repository.has_commit():
            return Baseline(None, None, notebook, b"", {}, {})
        commit = self.repository.read_head()
        specs = self.repository.read_tree(commit, paths.SPECS_DIR)
        accepted = self.repository.find_commit(ACCEPTANCE_TRAILER)
        if accepted is None:
            if specs:
                raise RepositoryStateError("Git holds specifications JRI did not write. Remove them before Ralphing.")
            return Baseline(commit, None, notebook, b"", {}, {})
        functional = self.repository.read_tree(accepted, paths.FUNCTIONAL_SPECS_DIR)
        architecture = self.repository.read_tree(accepted, paths.ARCHITECTURE_SPECS_DIR)
        if specs != functional | architecture:
            raise RepositoryStateError("Checked-out specifications differ from the ones JRI accepted.")
        logger.info("baseline_prepared head=%s accepted=%s functional=%d", commit, accepted, len(functional))
        return Baseline(
            commit,
            accepted,
            notebook,
            self.repository.read_file(accepted, paths.NOTEBOOK_FILE),
            functional,
            architecture,
        )

    def apply(self, repository: git.Repository, patch: str, model_root: str) -> None:
        self._validate_patch(patch, model_root)
        try:
            # A model writes hunks with no trailing context, which Git
            # otherwise takes for a patch against the end of a file and
            # refuses anywhere else. The lines a hunk quotes still have
            # to be in the file, `_validate_patch` above still allows no
            # path but a Markdown specification under the model's own
            # root, and the worktree this lands in is one the run throws
            # away -- so what a hunk gains is the freedom to sit
            # elsewhere in a file JRI wrote and is about to diff.
            repository.apply_patch(patch.encode(), index=True, directory=paths.SPECS_DIR, zero_context=True)
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
        if self._read_notebook() != baseline.notebook:
            raise RepositoryStateError("The project notes changed during generation. Try again.")
        self._check_state()
        # Written down before the project is touched, so that undoing
        # this acceptance never has to be worked out from what a run
        # left behind -- least of all by a run that is not the one that
        # left it.
        acceptance = Acceptance(
            accepted=baseline.accepted,
            patch=patch.decode(),
            indexed=self.repository.read_staged_paths(paths.COMMITTED_PATHS),
        )
        self._record_acceptance(acceptance)
        self.repository.apply_patch(patch)
        try:
            # The intent alone, so JRI never writes over content the
            # user staged for a path of its own. What the project
            # ignores does not decide this: `.jri` is JRI's to keep in
            # Git, and a project that ignores it Ralphs like any other
            # rather than failing once the generation has run.
            self.repository.stage(paths.COMMITTED_PATHS, intent_to_add=True, force=True)
            commit = self.repository.commit(
                "jri: update specifications", trailers=(ACCEPTANCE_TRAILER,), paths=paths.COMMITTED_PATHS
            )
        except git.Error:
            self._undo_acceptance(acceptance)
            raise
        self.workspace.acceptance_file.unlink(missing_ok=True)
        logger.info("specs_committed commit=%s", commit)
        return commit

    # An acceptance the operating system killed halfway leaves JRI's
    # own specifications in the worktree with no commit holding them,
    # and every later run refuses to start over them. The offer that
    # starts a run stands through that refusal, so without this the
    # only way out is for the user to delete files JRI wrote.
    def _reconcile(self) -> None:
        acceptance = self._read_acceptance()
        if acceptance is None:
            return
        accepted = self.repository.find_commit(ACCEPTANCE_TRAILER) if self.repository.has_commit() else None
        # The kill may have landed after Git wrote the commit, and past
        # that point the patch is the project's: reversing it would
        # delete specifications the user has.
        if accepted != acceptance.accepted:
            self.workspace.acceptance_file.unlink(missing_ok=True)
            logger.info("acceptance_committed commit=%s", accepted)
            return
        try:
            self.repository.apply_patch(acceptance.patch.encode(), reverse=True, check=True)
        except git.Error:
            # What is there is no longer what JRI wrote, so it is not
            # JRI's to remove. The record stays, and whatever the user
            # has to sort out `_check_state` names below.
            logger.info("acceptance_undo_refused accepted=%s", acceptance.accepted)
            return
        self._undo_acceptance(acceptance)

    def _record_acceptance(self, acceptance: Acceptance) -> None:
        self.workspace.open_generation_dir()
        files.write_atomically(self.workspace.acceptance_file, acceptance.model_dump_json())

    def _read_acceptance(self) -> Acceptance | None:
        if not self.workspace.acceptance_file.exists():
            return None
        return Acceptance.model_validate_json(self.workspace.acceptance_file.read_bytes())

    def _undo_acceptance(self, acceptance: Acceptance) -> None:
        # Only the entries the acceptance staged come back out.
        # Resetting a path the user had staged themselves would throw
        # their content away instead, since Git puts back whatever HEAD
        # holds for it, and nothing at all when HEAD does not hold it
        # yet.
        added = [
            path for path in self.repository.read_staged_paths(paths.COMMITTED_PATHS) if path not in acceptance.indexed
        ]
        if added:
            self.repository.unstage(added)
        self.repository.apply_patch(acceptance.patch.encode(), reverse=True)
        self.workspace.acceptance_file.unlink(missing_ok=True)
        logger.info("acceptance_undone unstaged=%d", len(added))

    def _read_notebook(self) -> bytes:
        try:
            return self.workspace.notebook_file.read_bytes()
        except OSError as error:
            logger.exception("notebook_read_failed path=%r", self.workspace.notebook_file)
            raise PersistenceError(
                f"Could not read the notebook file `{self.workspace.notebook_file}`: {error.strerror}"
            ) from error

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
        # The staging reaches past whatever the project ignores, so
        # this reaches exactly as far, and over exactly what that
        # staging takes: a file of the user's under a path of JRI's
        # stays theirs however Git was told to treat it, and a check
        # blind to those rules would let the commit sweep it up.
        blockers = sorted(entry.path for entry in self.repository.read_status((paths.COMMITTED_SPECS,), ignored=True))
        if blockers:
            raise RepositoryStateError(
                "Commit or remove these files before Ralphing:\n" + "\n".join(f"- {path}" for path in blockers)
            )
        # What `_validate_patch` refuses inside a patch, the files JRI
        # commits may not be either. Git records a link as the text of
        # its target, so the notebook comes back out of the commit as a
        # path where the notes should be, and a specification read out
        # of a worktree is whatever the link points at -- a file that
        # was never JRI's to show a model. These are `COMMITTED_PATHS`
        # again, as the filesystem rather than Git spells them.
        committed = (
            self.workspace.config_file,
            self.workspace.gitignore_file,
            self.workspace.notebook_file,
            *(self.workspace.root / paths.SPECS_DIR).rglob("*.md"),
        )
        links = sorted(path.relative_to(self.workspace.root).as_posix() for path in committed if path.is_symlink())
        if links:
            raise RepositoryStateError(
                "JRI writes plain files, and these are links. Replace them before Ralphing:\n"
                + "\n".join(f"- {path}" for path in links)
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
