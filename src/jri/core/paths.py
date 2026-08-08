PROJECT_GITIGNORE_FILE = ".gitignore"

WORKSPACE_DIR = ".jri"

CONFIG_FILE = f"{WORKSPACE_DIR}/config.yaml"
GITIGNORE_FILE = f"{WORKSPACE_DIR}/.gitignore"
NOTEBOOK_FILE = f"{WORKSPACE_DIR}/notebook.yaml"
SESSION_FILE = f"{WORKSPACE_DIR}/session.json"
VISUALIZATION_FILE = f"{WORKSPACE_DIR}/visualization.html"

GENERATION_DIR = f"{WORKSPACE_DIR}/generation"

ACCEPTANCE_FILE = f"{GENERATION_DIR}/acceptance.json"
# A rename writes the record, and a lock is held on the file that was
# renamed away rather than on the name, so what an acceptance is held
# by is a file no write of JRI's ever replaces.
ACCEPTANCE_LOCK_FILE = f"{GENERATION_DIR}/acceptance.lock"
DRAFT_FILE = f"{GENERATION_DIR}/draft.patch"

LOGS_DIR = f"{WORKSPACE_DIR}/logs"

LOG_FILE = f"{LOGS_DIR}/jri.log"
# Rotation renames the log, so what the runs of a session take turns
# over is a file no rename ever moves out from under them.
LOG_LOCK_FILE = f"{LOGS_DIR}/.lock"

SPECS_DIR = f"{WORKSPACE_DIR}/specs"

# Specification roots as the models see them, relative to `SPECS_DIR`.
ARCHITECTURE_SPECS_ROOT = "architecture"
FUNCTIONAL_SPECS_ROOT = "functional"

ARCHITECTURE_SPECS_DIR = f"{SPECS_DIR}/{ARCHITECTURE_SPECS_ROOT}"
FUNCTIONAL_SPECS_DIR = f"{SPECS_DIR}/{FUNCTIONAL_SPECS_ROOT}"

# The specifications answer to a pattern rather than to the directory
# holding them, so that reaching past a project's ignore rules takes
# the Markdown JRI wrote and never what else those rules were hiding.
# `:(glob)` is how Git spells a pattern that crosses directories.
COMMITTED_SPECS = f":(glob){SPECS_DIR}/**/*.md"

# Everything JRI commits, and all a JRI commit ever holds.
COMMITTED_PATHS = (CONFIG_FILE, GITIGNORE_FILE, NOTEBOOK_FILE, COMMITTED_SPECS)

RESET_PATHS = (SESSION_FILE, NOTEBOOK_FILE, VISUALIZATION_FILE, LOGS_DIR, SPECS_DIR, GENERATION_DIR)
