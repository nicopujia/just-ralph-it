PROJECT_GITIGNORE_FILE = ".gitignore"

WORKSPACE_DIR = ".jri"

CONFIG_FILE = f"{WORKSPACE_DIR}/config.yaml"
GITIGNORE_FILE = f"{WORKSPACE_DIR}/.gitignore"
NOTEBOOK_FILE = f"{WORKSPACE_DIR}/notebook.yaml"
SESSION_FILE = f"{WORKSPACE_DIR}/session.json"
VISUALIZATION_FILE = f"{WORKSPACE_DIR}/visualization.html"

# This lock lets one chat hold a project. It contains the holder process PID.
LOCK_FILE = f"{WORKSPACE_DIR}/lock"
# The claim separates lock acquisition from holder recording. A reader under this claim reads the current holder record.
CLAIM_FILE = f"{WORKSPACE_DIR}/lock.claim"

GENERATION_DIR = f"{WORKSPACE_DIR}/generation"

ACCEPTANCE_FILE = f"{GENERATION_DIR}/acceptance.json"
# A rename writes the record, but the lock remains on the renamed file. JRI never replaces an acceptance lock file.
ACCEPTANCE_LOCK_FILE = f"{GENERATION_DIR}/acceptance.lock"
DRAFT_FILE = f"{GENERATION_DIR}/draft.patch"
# The run appends one journal line at a time. A killed run leaves all earlier lines readable.
JOURNAL_FILE = f"{GENERATION_DIR}/journal.jsonl"
# The runner holds this lock while it runs. Other processes use it to find whether the runner is still active.
GENERATION_LOCK_FILE = f"{GENERATION_DIR}/lock"
# This file requests a stop from another process.
# The runner polls it because a signal cannot reach its Windows process group.
# Use this method on all platforms.
CANCEL_FILE = f"{GENERATION_DIR}/cancel"
# This file holds runner output outside its log, such as an interpreter traceback or standard-error output.
# It reports a crash before journaling starts.
RUNNER_LOG_FILE = f"{GENERATION_DIR}/runner.log"

LOGS_DIR = f"{WORKSPACE_DIR}/logs"

LOG_FILE = f"{LOGS_DIR}/jri.log"
# Log rotation renames log files. This lock file remains in place while session runs use it.
LOG_LOCK_FILE = f"{LOGS_DIR}/.lock"

SPECS_DIR = f"{WORKSPACE_DIR}/specs"

# These are specification roots as models see them, relative to `SPECS_DIR`.
ARCHITECTURE_SPECS_ROOT = "architecture"
FUNCTIONAL_SPECS_ROOT = "functional"

# These are all specification roots for reads that receive a path instead of an agent root.
SPECS_ROOTS = (ARCHITECTURE_SPECS_ROOT, FUNCTIONAL_SPECS_ROOT)

ARCHITECTURE_SPECS_DIR = f"{SPECS_DIR}/{ARCHITECTURE_SPECS_ROOT}"
FUNCTIONAL_SPECS_DIR = f"{SPECS_DIR}/{FUNCTIONAL_SPECS_ROOT}"

# Use a pattern, not the directory, so Git includes only JRI Markdown and excludes files hidden by project ignore rules.
# `:(glob)` matches directories.
COMMITTED_SPECS = f":(glob){SPECS_DIR}/**/*.md"

# These are all paths that JRI commits.
COMMITTED_PATHS = (CONFIG_FILE, GITIGNORE_FILE, NOTEBOOK_FILE, COMMITTED_SPECS)

RESET_PATHS = (SESSION_FILE, NOTEBOOK_FILE, VISUALIZATION_FILE, LOGS_DIR, SPECS_DIR, GENERATION_DIR)
