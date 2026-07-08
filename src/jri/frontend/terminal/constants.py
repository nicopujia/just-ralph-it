from jri.core.agents import Interviewer

# Maintain constants ordered alphabetically with one blank line between
# different initial letter.

CONFIG_ERROR_COPY = """Invalid configuration.
Set or fix these settings:

{errors}

You can define them in your shell, in a .env file in this directory,
or pass them as CLI flags.
"""

INTERVIEWER_ERROR_COPY = """
Something went wrong while talking to the interviewer:

{error}

Please try again or contact Nico.
""".strip()
INTERVIEWER_MESSAGE_CLASSES = "interviewer-message"
INTERVIEWER_NO_RESPONSE_COPY = "_No response received._"
INTERVIEWER_THINKING_COPY = "_Thinking..._"
INTERVIEWER_TURN_CLASSES = "interviewer-turn"

MESSAGES_CONTAINER_ID = "messages"
MESSAGE_INPUT_ID = "message-input"
MESSAGE_INPUT_INITIAL_PLACEHOLDER_COPY = Interviewer.FIRST_MESSAGE
MESSAGE_INPUT_PLACEHOLDER_COPY = "Share your thoughts"

THEME_DARK = "ansi-dark"
THEME_DEFAULT = THEME_DARK
THEME_LIGHT = "ansi-light"
TITLE_COPY = "Just Ralph It"
TOOL_CALL_ROW_CLASSES = "tool-call-row"

USER_MESSAGE_CLASSES = "user-message"

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

#{MESSAGE_INPUT_ID} {{
    height: auto;
    max-height: 16;
    margin: 1;
}}

.{USER_MESSAGE_CLASSES} {{
    text-style: bold;
    margin-bottom: 1;
    margin-top: 1;
    padding: 1;
    border-left: heavy ansi_bright_magenta;
}}

.{INTERVIEWER_TURN_CLASSES} {{
    height: auto;
    padding-top: 1;
}}

.{TOOL_CALL_ROW_CLASSES} {{
    margin-bottom: 1;
    padding-right: 2;
    padding-left: 2;
    text-opacity: 70%;
    text-style: dim;
}}
""".strip()
