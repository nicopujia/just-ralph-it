from jri.core import issues
from jri.core.ai import Interviewer

# Keep alphabetical order. Put a blank line between initial letters.

AUTH_ERROR = "Authentication failed: {error}"

CANCEL_TURN = "Stop response"
CANCEL_TURN_CONFIRMATION = "Press Esc again to stop"
CANCEL_TURN_KEY = "esc esc"
CANCEL_TURN_STARTED = "Stopping response…"
CLI_CHAT_HELP = "Chat with the interviewer in the terminal UI."
CLI_FORCE_HELP = (
    "Re-create the JRI workspace: write the settings file again and delete the conversation, the notes, "
    "the logs, the generated specifications, and what the last Just Ralph It run left behind."
)
CLI_INIT_HELP = "Set the project up with the default JRI settings."
CLI_NO_COMMENTS_HELP = "Write the settings file with no comments and only the settings that have a value."
CLI_VERSION_HELP = "Show the JRI version and exit."
CLI_VIEW_HELP = "Visualize the notes graph."
CLI_YES_HELP = "Answer yes to the confirmation --force asks for."
CLOSE_SHORTCUTS = "Close"
CLOSE_SHORTCUTS_KEY = "Esc"
COMMAND_PALETTE = "Command palette"

FORCE_CANCELLED = "Nothing was deleted."
FORCE_PROMPT = "Type `y` to continue, or anything else to cancel: "
FORCE_WARNING = """--force replaces these, and what they hold cannot be brought back:

{paths}
"""

GIT_ERROR = "Git failed: {error}"

HIDE_THINKING_BLOCKS = "Hide thinking blocks"

INIT_COMMITTED = "Committed the workspace files to Git."
INIT_CREATED = "Created a JRI workspace at {directory}, with its settings, notebook, logs, and Git ignores."
INIT_EXISTING = "A JRI workspace already exists at {directory}."
INIT_NEXT_STEPS = "Open {settings_file} and follow its comments, then run `jri chat`."
INIT_RECREATED = (
    "Re-created the JRI workspace at {directory}, replacing its settings, notes, conversation, logs, "
    "specifications, and the record of the last Just Ralph It run."
)
INIT_REPOSITORY = "Initialized a Git repository here."
INSERT_NEWLINE = "Insert newline"
INTERNAL_ERROR = "Something unexpected went wrong. Check the JRI log for details."
INTERVIEWER_STOPPING = "Stopping..."
INTERVIEWER_THINKING = "Thinking..."

KEYMAP_PANEL = "Full keymap"

MESSAGE_INPUT_INITIAL_PLACEHOLDER = Interviewer.FIRST_MESSAGE
MESSAGE_INPUT_PLACEHOLDER = "Share your thoughts"

NEXT_COMMAND = "Next command"

PERSISTENCE_ERROR = "Persistence failed: {error}"

QUIT = "Quit"
QUIT_COMMAND = ":q"
QUIT_CONFIRMATION = "Press ^q again to stop this and quit"

RALPHING = "Ralphing... [dim](this will take a long time... you may close this window)[/dim]"
RALPHING_THINKING_HINT = "Press ^t to watch the models think."
RALPH_BUTTON = "Just Ralph It"
RALPH_KEY = "^x j"
RALPH_LETTER = "j"
REDO = "Redo"
REDO_MESSAGE = "Redo message"
REDO_MESSAGE_KEY = "^x r"
REDO_MESSAGE_LETTER = "r"
RETRY = "Try again"
RETRY_KEY = "^x t"
RETRY_LETTER = "t"
RUN_CANCELLATION_CONFIRM = "Stop the run"
RUN_CANCELLATION_DECLINE = "Keep ralphing"
RUN_CANCELLATION_QUESTION = (
    "[b]Stop this run?[/b]\n\n"
    "[dim]It stops where it stands and puts nothing into your project. A later run starts again from the "
    "specifications this one drafted.[/dim]"
)

SEND_MESSAGE = "Send message"
SEND_MESSAGE_CONFIRMATION = "Press Enter again to stop this and send"
SETTINGS_ERROR = """Invalid settings.
Set or fix these settings in .jri/settings.yaml:

{errors}

All commands, including `jri view`, use these settings.
"""
SHORTCUTS = "Shortcuts"
SHOW_THINKING_BLOCKS = "Show thinking blocks"

THINKING_BLOCKS = "Thinking blocks"
THINKING_BLOCKS_COMMAND = "Toggle model's chain-of-thought (reasoning) text blocks."
TITLE = "Just Ralph It"
TOOL_CALL_DETAILED = "{label} — {detail}"
TOOL_CALL_EMPTY_SYMBOL = "○"
TOOL_CALL_FAILED = "{label} — failed"
TOOL_CALL_FAILED_SYMBOL = "✗"
TOOL_CALL_STOPPED = "{label} — stopped"
TOOL_CALL_STOPPED_SYMBOL = "⊘"
TOOL_CALL_STOPPING = "{label} — stopping…"
TOOL_CALL_UNFINISHED = "{label} — unfinished"
TOOL_CALL_UNFINISHED_SYMBOL = "⋯"
TURN_BLOCKED = "Stopped because of your project's state:\n\n{error}\n\nSort that out and try again."
TURN_ERROR = (
    f"Something went wrong:\n\n{{error}}\n\nTry again. If it keeps happening, report it at {issues.URL} "
    "and attach the .jri/ directory as a zip."
)
TURN_EXHAUSTED = "Your ChatGPT or API usage limit has been reached. Check your plan and try again later."
TURN_INTERRUPTED = "_JRI closed before this finished._"
TURN_NO_RESPONSE = "_No response received._"
TURN_REFUSED = (
    "{error}\n\n"
    "The provider refused the request rather than failing at it, so asking for it again changes nothing on its own. "
    "What JRI asks with is in .jri/settings.yaml: llm.provider, llm.api_key, and each agent's model, reasoning effort "
    "and temperature. Change what needs changing there, then start JRI again."
)
TURN_STOPPED = "_Response stopped._"
TURN_UNAVAILABLE = (
    "{error}\n\n"
    "llm.provider in .jri/settings.yaml decides the address JRI sends to. Check that it is the one you meant and that "
    "this machine can reach it, then try again."
)

UNDO_MESSAGE = "Undo message"
UNDO_MESSAGE_KEY = "^x u"
UNDO_MESSAGE_LETTER = "u"
UNKNOWN_SETTING = "There is no such setting."
UNKNOWN_SETTING_SUGGESTION = "There is no such setting. Did you mean {setting}?"

VIEW_NEXT_STEPS = "The page is a snapshot: run `jri view` again after `jri chat` to redraw it."
VIEW_NO_NOTES = "The notebook has no notes yet. Run `jri chat` to start the interview."
VIEW_OPENED = "Wrote the notes graph to {file} and asked your browser to open it."
VIEW_UNOPENED = "Wrote the notes graph to {file}. Open it in a browser to see it."

WORKSPACE_HELD = """Another JRI is already open in this project, in the window running process {holder}.

One JRI at a time writes the notes and the conversation, so this one cannot start beside it.

Taking over kills that window where it stands: the reply it is writing is lost, and the terminal it was drawing
in will need `reset`. What it has already saved is kept: the notes, the conversation, and the specifications a
run had drafted. A Just Ralph It run is a process of its own, so it keeps going, and this window picks it up.

The answer comes from a lock in `.jri`, which JRI takes to be on a local disk. Over a network filesystem it is
not dependable.
"""
WORKSPACE_HELD_KEPT = "The other window is still running, so this one did not start."
WORKSPACE_HELD_PROMPT = "Type `y` to kill it and take over, or anything else to leave it running: "
WORKSPACE_HELD_STANDING = "The other window did not let the project go. Close it, then run `jri chat` again."
WORKSPACE_MISSING = "No JRI workspace here. Run `jri init` to create one."
