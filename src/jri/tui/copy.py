from jri.core.ai import Interviewer

# Maintain constants ordered alphabetically with one blank line between
# different initial letters.

AUTH_ERROR = "Authentication failed: {error}"

CANCEL_TURN = "Stop response"
CANCEL_TURN_CONFIRMATION = "Press Esc again to stop"
CANCEL_TURN_KEY = "esc esc"
CANCEL_TURN_STARTED = "Stopping response…"
CLI_COMMAND_HELP = (
    "init: set the project up with the default JRI configuration. "
    "chat: chat with the interviewer in the terminal UI. "
    "view: visualize the notes graph."
)
CLI_EPILOG = (
    "Every setting documented in .jri/config.yaml also works here as a flag (--llm.provider, "
    "--agents.interviewer.model, ...) or as an environment variable (JRI_LLM_PROVIDER, "
    "JRI_AGENTS_INTERVIEWER_MODEL, ...), before or after the command."
)
CLI_VERSION_HELP = "Show the JRI version and exit."
CONFIG_ERROR = """Invalid configuration.
Set or fix these settings:

{errors}

You can define them in .jri/config.yaml,
your shell, a .env file, or CLI flags.
All commands, including `jri view`, use this configuration.
"""

FORCE_CANCELLED = "Nothing was deleted."
FORCE_COMMAND = "--force only applies to `jri init`."
FORCE_PROMPT = "Type `y` to continue, or anything else to cancel: "
FORCE_WARNING = """--force replaces these, and what they hold cannot be brought back:

{paths}
"""

GIT_ERROR = "Git failed: {error}"

HIDE_THINKING_BLOCKS = "Hide thinking blocks"

INIT_CREATED = "Created a JRI workspace at {directory}, with its configuration, notebook, logs, and Git ignores."
INIT_EXISTING = "A JRI workspace already exists at {directory}."
INIT_NEXT_STEPS = "Open {config_file} and follow its comments, then run `jri chat`."
INIT_RECREATED = (
    "Re-created the JRI workspace at {directory}, replacing its configuration, notes, conversation, logs, "
    "and specifications."
)
INIT_REPOSITORY = (
    "Initialized a Git repository at {directory} and committed what was already there, since JRI builds on commits."
)
INSERT_NEWLINE = "Insert newline"
INTERNAL_ERROR = "Something unexpected went wrong. Check the JRI log for details and try again."
INTERVIEWER_ERROR = """
Something went wrong while talking to the interviewer:

{error}

Please try again or contact Nico.
""".strip()
INTERVIEWER_NO_RESPONSE = "_No response received._"
INTERVIEWER_STOPPED = "_Response stopped._"
INTERVIEWER_THINKING = "_Thinking..._"

KEYMAP_PANEL = "Keymap"

LLM_USAGE_LIMIT = "Your ChatGPT or API usage limit has been reached. Check your plan and try again later."

MESSAGE_HISTORY = "Message history"
MESSAGE_INPUT_INITIAL_PLACEHOLDER = Interviewer.FIRST_MESSAGE
MESSAGE_INPUT_PLACEHOLDER = "Share your thoughts"

NEXT_COMMAND = "Next command"

PERSISTENCE_ERROR = "Persistence failed: {error}"

RALPHING = "Ralphing... [dim](don't close this window)[/dim]"
RALPH_BLOCKED = "Ralphing stopped because of your project's state:\n\n{error}\n\nSort that out and Ralph again."
RALPH_BUTTON = "Just Ralph It"
RALPH_ERROR = "Could not finish specifications:\n\n{error}"
RALPH_INTERRUPTED = "Could not finish specifications"
RALPH_KEY = "^x j"
REDO = "Redo"
REDO_MESSAGE = "Redo message"
REDO_MESSAGE_KEY = "^x r"
RETRY = "Try again"
RETRY_KEY = "^x t"

SEND_MESSAGE = "Send message"
SHOW_THINKING_BLOCKS = "Show thinking blocks"

THINKING_BLOCKS = "Thinking blocks"
THINKING_BLOCKS_COMMAND = "Toggle model's chain-of-thought (reasoning) text blocks."
TITLE = "Just Ralph It"

UNDO_MESSAGE = "Undo message"
UNDO_MESSAGE_KEY = "^x u"

WORKSPACE_MISSING = "No JRI workspace here. Run `jri init` to create one."
