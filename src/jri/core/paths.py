WORKSPACE_DIR = ".jri"

SETTINGS_FILE = f"{WORKSPACE_DIR}/settings.yaml"
# The user writes this file by hand. `jri init` copies its settings into each new project.
GLOBAL_SETTINGS_FILE = f"~/{SETTINGS_FILE}"
GITIGNORE_FILE = f"{WORKSPACE_DIR}/.gitignore"
NOTEBOOK_FILE = f"{WORKSPACE_DIR}/notebook.yaml"
# This is the notebook of the last run that wrote specifications. The next run reads what changed since then.
# It stays out of the ignore rules, so a commit of the user carries it with the specifications.
ACCEPTED_NOTEBOOK_FILE = f"{WORKSPACE_DIR}/accepted-notebook.yaml"
SESSION_FILE = f"{WORKSPACE_DIR}/session.json"
VISUALIZATION_FILE = f"{WORKSPACE_DIR}/visualization.html"

# This lock lets one chat hold a project. It contains the pid of the holder process.
LOCK_FILE = f"{WORKSPACE_DIR}/lock"
# The claim separates the step that takes the lock from the step that records the holder.
# A reader that holds this claim reads the current holder record.
CLAIM_FILE = f"{WORKSPACE_DIR}/lock.claim"

GENERATION_DIR = f"{WORKSPACE_DIR}/generation"

DRAFT_FILE = f"{GENERATION_DIR}/draft.patch"
# The run appends one journal line at a time. A run that stops leaves all earlier lines readable.
JOURNAL_FILE = f"{GENERATION_DIR}/journal.jsonl"
# The runner holds this lock while it runs. Other processes use it to find whether the runner is still active.
GENERATION_LOCK_FILE = f"{GENERATION_DIR}/lock"
# This file requests a stop from another process.
# The runner polls it because a signal cannot reach its Windows process group.
# Use this method on all platforms.
CANCEL_FILE = f"{GENERATION_DIR}/cancel"
# This file holds runner output outside its log, such as an interpreter traceback or standard-error output.
# It reports a crash before the runner starts to write the journal.
RUNNER_LOG_FILE = f"{GENERATION_DIR}/runner.log"
# The explorer studies a disposable copy of the project here.
SNAPSHOT_DIR = f"{GENERATION_DIR}/snapshot"

# A run works in a private repository here, beside the project, and not in a system temporary directory.
# It belongs to the run that opened it, and that run removes it when it ends.
WORKTREE_DIR = f"{WORKSPACE_DIR}/worktree"

LOGS_DIR = f"{WORKSPACE_DIR}/logs"

# One file holds the whole session. A trim drops its oldest records to keep it inside its size limit.
LOG_FILE = f"{LOGS_DIR}/session.log"
# Session runs take this lock to write the log file. A trim rewrites that file, and this lock stays where it is.
LOG_LOCK_FILE = f"{LOGS_DIR}/lock"

SPECS_DIR = f"{WORKSPACE_DIR}/specs"

# These are specification roots as models see them, relative to `SPECS_DIR`.
ARCHITECTURE_SPECS_ROOT = "architecture"
FUNCTIONAL_SPECS_ROOT = "functional"

# These are all the specification roots.
# JRI uses them when it receives a path instead of an agent root.
SPECS_ROOTS = (ARCHITECTURE_SPECS_ROOT, FUNCTIONAL_SPECS_ROOT)

ARCHITECTURE_SPECS_DIR = f"{SPECS_DIR}/{ARCHITECTURE_SPECS_ROOT}"
FUNCTIONAL_SPECS_DIR = f"{SPECS_DIR}/{FUNCTIONAL_SPECS_ROOT}"

# These files must not be links. Use a pattern, not the directory. Git then reads only the JRI Markdown files.
# `:(glob)` matches directories.
PLAIN_PATHS = (SETTINGS_FILE, GITIGNORE_FILE, NOTEBOOK_FILE, ACCEPTED_NOTEBOOK_FILE, f":(glob){SPECS_DIR}/**/*.md")

RESET_PATHS = (
    SESSION_FILE,
    NOTEBOOK_FILE,
    ACCEPTED_NOTEBOOK_FILE,
    VISUALIZATION_FILE,
    LOGS_DIR,
    SPECS_DIR,
    GENERATION_DIR,
    WORKTREE_DIR,
)
