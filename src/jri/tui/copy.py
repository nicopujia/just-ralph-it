from jri.core.ai import Interviewer

# Alphabetical order, blank line between initial letters

AUTH_ERROR = "Authentication failed: {error}"

CANCEL_TURN = "Stop response"
CANCEL_TURN_CONFIRMATION = "Press Esc again to stop"
CANCEL_TURN_KEY = "esc esc"
CANCEL_TURN_STARTED = "Stopping response…"
CLI_CHAT_HELP = "Chat with the interviewer in the terminal UI."
CLI_FORCE_HELP = (
    "Re-create the JRI workspace: write the configuration file again and delete the conversation, the notes, "
    "the logs, and the generated specifications."
)
CLI_INIT_HELP = "Set the project up with the default JRI configuration."
CLI_VERSION_HELP = "Show the JRI version and exit."
CLI_VIEW_HELP = "Visualize the notes graph."
CLI_YES_HELP = "Answer yes to the confirmation --force asks for."
CONFIG_ERROR = """Invalid configuration.
Set or fix these settings in .jri/config.yaml:

{errors}

All commands, including `jri view`, use this configuration.
"""

FORCE_CANCELLED = "Nothing was deleted."
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
TOOL_CALL_FAILED = "{label} — failed"
TOOL_CALL_FAILED_SYMBOL = "✗"

UNDO_MESSAGE = "Undo message"
UNDO_MESSAGE_KEY = "^x u"
UNKNOWN_SETTING = "There is no such setting."
UNKNOWN_SETTING_SUGGESTION = "There is no such setting. Did you mean {setting}?"

VISUALIZATION_DRAW_ERROR = "The graph viewer loaded, but it could not draw the graph. Please contact Nico."
VISUALIZATION_LOAD_ERROR = (
    "The graph viewer could not load what it needs from the internet. Check your connection and run `jri view` again."
)

WORKSPACE_MISSING = "No JRI workspace here. Run `jri init` to create one."
