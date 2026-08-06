# Alphabetical order, blank line between initial letters

INTERVIEWER_ERROR_CLASSES = "interviewer-error"
INTERVIEWER_MESSAGE_CLASSES = "interviewer-message"
INTERVIEWER_REASONING_CLASSES = "interviewer-reasoning"
INTERVIEWER_TURN_CLASSES = "interviewer-turn"

MESSAGES_CONTAINER_ID = "messages"
MESSAGE_INPUT_ID = "message-input"

RALPH_BUTTON_CLASSES = "ralph-button"
RALPHING_CLASSES = "ralphing"

RETRY_BUTTON_CLASSES = "retry-button"

SHORTCUT_HINTS_CLASSES = "shortcut-hints"

THEME_DARK = "ansi-dark"
THEME_LIGHT = "ansi-light"
TOOL_CALL_ROW_CLASSES = "tool-call-row"
TOOL_CALL_ROW_FAILED_CLASSES = "tool-call-row-failed"

USER_MESSAGE_CLASSES = "user-message"

# f-strings using other constants go below

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

#{MESSAGE_INPUT_ID}, .{RALPHING_CLASSES} {{
    height: auto;
    max-height: 16;
    margin: 1;
}}

/* The panel stands where the message input stood, so it is that box:
   the border, padding and colours below are the ones the text area
   draws for itself, which no rule of ours can reach. */
.{RALPHING_CLASSES} {{
    display: none;
    padding: 0 1;
    border: tall $border;
    background: $surface;
    color: $foreground;
    content-align: center middle;
}}

.{RALPHING_CLASSES} LoadingIndicator {{
    width: 12;
    height: 1;
}}

.{RALPHING_CLASSES} Static {{
    width: auto;
    height: 1;
}}

.{SHORTCUT_HINTS_CLASSES} {{
    display: none;
    dock: bottom;
    height: 1;
    padding-left: 1;
    background: $footer-background;
    color: $footer-foreground;
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

.{INTERVIEWER_ERROR_CLASSES} {{
    margin-bottom: 1;
    padding: 1;
    border-left: heavy ansi_bright_red;
}}

.{INTERVIEWER_ERROR_CLASSES} MarkdownBlock:last-child {{
    margin-bottom: 0;
}}

.{RETRY_BUTTON_CLASSES} {{
    margin-bottom: 1;
    margin-left: 2;
    background: $error;
    color: $ansi-background;
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

.{TOOL_CALL_ROW_FAILED_CLASSES} {{
    color: ansi_bright_red;
    text-opacity: 100%;
}}
""".strip()
