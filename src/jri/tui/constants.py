from jri.core.ai import Interviewer

# Maintain constants ordered alphabetically with one blank line between
# different initial letter.

CONFIG_ERROR_COPY = """Invalid configuration.
Set or fix these settings:

{errors}

You can define them in .jri/config.yaml,
your shell, a .env file, or CLI flags.
All commands, including `jri view`, use this configuration.
"""

HISTORY_BATCH_SIZE = 15

INIT_CREATED_COPY = "Created a JRI workspace at {directory}, with its configuration and Git ignores."
INIT_EXISTING_COPY = "A JRI workspace already exists at {directory}."
INIT_NEXT_STEPS_COPY = "Open {config_file} and follow its comments, then run `jri chat`."
INIT_REPOSITORY_COPY = "Initialized an empty Git repository at {directory}, since JRI commits what it writes."

INTERVIEWER_ERROR_COPY = """
Something went wrong while talking to the interviewer:

{error}

Please try again or contact Nico.
""".strip()
INTERVIEWER_MESSAGE_CLASSES = "interviewer-message"
INTERVIEWER_NO_RESPONSE_COPY = "_No response received._"
INTERVIEWER_REASONING_CLASSES = "interviewer-reasoning"
INTERVIEWER_STOPPED_COPY = "_Response stopped._"
INTERVIEWER_THINKING_COPY = "_Thinking..._"
INTERVIEWER_TURN_CLASSES = "interviewer-turn"

INTERNAL_ERROR_COPY = "Something unexpected went wrong. Check the JRI log for details and try again."

LLM_USAGE_LIMIT_COPY = "Your ChatGPT or API usage limit has been reached. Check your plan and try again later."

MESSAGES_CONTAINER_ID = "messages"
MESSAGE_INPUT_ID = "message-input"
MESSAGE_INPUT_INITIAL_PLACEHOLDER_COPY = Interviewer.FIRST_MESSAGE
MESSAGE_INPUT_PLACEHOLDER_COPY = "Share your thoughts"

RALPH_BUTTON_CLASSES = "ralph-button"
RALPH_BUTTON_COPY = "Just Ralph It"
RALPH_ERROR_COPY = "Could not finish specifications:\n\n{error}"
RALPHING_CLASSES = "ralphing"
RALPHING_COPY = "Ralphing... [dim](don't close this window)[/dim]"

RETRY_BUTTON_CLASSES = "retry-button"
RETRY_COPY = "Try again"

THEME_DARK = "ansi-dark"
THEME_LIGHT = "ansi-light"
TITLE_COPY = "Just Ralph It"
TOOL_CALL_ROW_CLASSES = "tool-call-row"

USER_MESSAGE_CLASSES = "user-message"

WORKSPACE_MISSING_COPY = "No JRI workspace here. Run `jri init` to create one."

# Formatted strings that need other constants go below

STYLESHEET = f"""
Screen {{
    layout: vertical;
}}

Header {{
    dock: top;
}}

#{MESSAGES_CONTAINER_ID} {{
    height: 1fr;
    padding-right: 2;
    padding-left: 2;
}}

#{MESSAGES_CONTAINER_ID} > Static {{
    height: 1fr;
}}

#{MESSAGE_INPUT_ID} {{
    height: auto;
    max-height: 16;
    margin: 1;
}}

.{RALPHING_CLASSES} {{
    display: none;
    height: 5;
    margin: 1;
    padding: 1;
    background: $primary;
    color: $ansi-background;
    content-align: center middle;
}}

.{RALPHING_CLASSES} LoadingIndicator {{
    width: 12;
    height: 1;
    color: $ansi-background;
}}

.{RALPHING_CLASSES} Static {{
    width: auto;
    height: 1;
}}

.{USER_MESSAGE_CLASSES} {{
    text-style: bold;
    margin-bottom: 1;
    margin-top: 1;
    padding: 1;
    border-left: heavy ansi_bright_magenta;
}}

.{USER_MESSAGE_CLASSES} MarkdownBlock:last-child {{
    margin-bottom: 0;
}}

.{INTERVIEWER_TURN_CLASSES} {{
    height: auto;
    padding-top: 1;
}}

.{INTERVIEWER_REASONING_CLASSES} {{
    padding-right: 2;
    padding-left: 2;
    text-opacity: 70%;
    text-style: dim italic;
}}

.{RETRY_BUTTON_CLASSES} {{
    margin-bottom: 1;
    margin-left: 2;
}}

.{RALPH_BUTTON_CLASSES} {{
    margin-bottom: 1;
    margin-left: 2;
    background: $warning;
    color: $ansi-background;
}}

.{TOOL_CALL_ROW_CLASSES} {{
    margin-bottom: 1;
    padding-right: 2;
    padding-left: 2;
    text-opacity: 70%;
    text-style: dim;
}}
""".strip()
