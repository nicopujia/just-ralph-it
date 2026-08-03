WORKSPACE_DIR = ".jri"

CONFIG_FILE = f"{WORKSPACE_DIR}/config.yaml"
GITIGNORE_FILE = f"{WORKSPACE_DIR}/.gitignore"
LOGS_DIR = f"{WORKSPACE_DIR}/logs"
NOTEBOOK_FILE = f"{WORKSPACE_DIR}/notebook.yaml"
SESSION_FILE = f"{WORKSPACE_DIR}/session.json"
VISUALIZATION_FILE = f"{WORKSPACE_DIR}/visualization.html"

SPECS_DIR = f"{WORKSPACE_DIR}/specs"

# Specification roots as the models see them, relative to `SPECS_DIR`.
ARCHITECTURE_SPECS_ROOT = "architecture"
FUNCTIONAL_SPECS_ROOT = "functional"

ARCHITECTURE_SPECS_DIR = f"{SPECS_DIR}/{ARCHITECTURE_SPECS_ROOT}"
FUNCTIONAL_SPECS_DIR = f"{SPECS_DIR}/{FUNCTIONAL_SPECS_ROOT}"

# Everything a forced run deletes to re-create the workspace.
RESET_PATHS = (SESSION_FILE, NOTEBOOK_FILE, VISUALIZATION_FILE, LOGS_DIR, SPECS_DIR)
