# Keep alphabetical order. Put a blank line between initial letters.

INPUT_BOX_ID = "input-box"
INTERVIEWER_BLOCKED_CLASSES = "interviewer-blocked"
INTERVIEWER_ERROR_CLASSES = "interviewer-error"
INTERVIEWER_MESSAGE_CLASSES = "interviewer-message"
INTERVIEWER_REASONING_CLASSES = "interviewer-reasoning"
INTERVIEWER_TURN_CLASSES = "interviewer-turn"

MESSAGES_CONTAINER_ID = "messages"
MESSAGE_INPUT_ID = "message-input"

RALPH_BUTTON_CLASSES = "ralph-button"
RALPHING_CLASSES = "ralphing"

RETRY_BUTTON_CLASSES = "retry-button"

RUN_ACTIVE_CLASSES = "run-active"
RUN_CANCELLATION_ANSWERS_CLASSES = "run-cancellation-answers"
RUN_CANCELLATION_CONFIRM_BUTTON_ID = "run-cancellation-confirm-button"
RUN_CANCELLATION_DIALOG_ID = "run-cancellation-dialog"

SHORTCUT_HINTS_CLASSES = "shortcut-hints"

THEME_DARK = "ansi-dark"
THEME_LIGHT = "ansi-light"
THINKING_LABEL_CLASSES = "thinking-label"
TOOL_CALL_ROW_CLASSES = "tool-call-row"
TOOL_CALL_ROW_FAILED_CLASSES = "tool-call-row-failed"

USER_MESSAGE_CLASSES = "user-message"

# Put f-strings that use other constants below.

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

/* The message input and panel share this space. This container sets its size. Both items fill it. */
#{INPUT_BOX_ID} {{
    margin: 1;
    height: auto;
    layers: message ralphing;
}}

/* A muted border tells the reader that the input does not take the keys.
   The theme blurred border is the background color of the terminal, which shows no box at all. */
#{MESSAGE_INPUT_ID} {{
    layer: message;
    height: auto;
    max-height: 16;
    border: tall ansi_bright_black;
}}

#{MESSAGE_INPUT_ID}:focus {{
    border: tall $border;
}}

/* The ANSI themes give muted text the color of normal text, thus no color can mute this hint.
   The terminal makes this gray from its own palette, as it does for the tool call rows. */
#{MESSAGE_INPUT_ID} > .text-area--placeholder {{
    text-style: dim;
}}

/* The panel is over the message input, so it uses this input container.
   The text area owns the border and padding below. These rules cannot change them.
   The input still sets the size for both items. A resize rewraps and moves both items together. */
.{RALPHING_CLASSES} {{
    layer: ralphing;
    display: none;
    height: 100%;
    padding: 0 1;
    border: tall $border;
    background: $surface;
    color: $foreground;
}}

.{RALPHING_CLASSES} LoadingIndicator {{
    width: 12;
    height: 1;
}}

.{RALPHING_CLASSES} Static {{
    width: auto;
    height: 1;
}}

/* A run gives the panel border, its dots, and the scrollbar the color of the Ralph button.
   The window then shows in one color that this run holds it. */
.{RUN_ACTIVE_CLASSES} .{RALPHING_CLASSES} {{
    border: tall $warning;
}}

.{RUN_ACTIVE_CLASSES} .{RALPHING_CLASSES} LoadingIndicator {{
    color: $warning;
}}

.{RUN_ACTIVE_CLASSES} #{MESSAGES_CONTAINER_ID} {{
    scrollbar-color: $warning;
    scrollbar-color-active: $warning;
    scrollbar-color-hover: $warning;
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

/* This label replaces a Markdown block, and it keeps the space that block used.
   The letters take their shades from the content, thus this rule sets only the slant. */
.{THINKING_LABEL_CLASSES} {{
    margin-bottom: 1;
    padding-right: 2;
    padding-left: 2;
    text-style: italic;
}}

.{INTERVIEWER_ERROR_CLASSES} {{
    margin-bottom: 1;
    padding: 1;
    border-left: heavy ansi_bright_red;
}}

.{INTERVIEWER_ERROR_CLASSES} MarkdownBlock:last-child {{
    margin-bottom: 0;
}}

/* A stop the user must clear is not a reply, so it takes a border of its own. Red shows a failure, and the run
   already has a color. Blue keeps its contrast on a light terminal and on a dark terminal. A light palette
   draws bright cyan too pale to see. */
.{INTERVIEWER_BLOCKED_CLASSES} {{
    margin-bottom: 1;
    padding: 1;
    border-left: heavy ansi_blue;
}}

.{INTERVIEWER_BLOCKED_CLASSES} MarkdownBlock:last-child {{
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

/* The dialog is a screen over the conversation. Put its box in the middle of that screen. */
RunCancellationDialog {{
    align: center middle;
}}

/* A narrow terminal gets the full width. A wide terminal keeps the question in one column. */
#{RUN_CANCELLATION_DIALOG_ID} {{
    width: 100%;
    max-width: 60;
    height: auto;
    padding: 1 2;
    border: tall $border;
    background: $surface;
}}

.{RUN_CANCELLATION_ANSWERS_CLASSES} {{
    height: auto;
    margin-top: 1;
    align-horizontal: right;
}}

.{RUN_CANCELLATION_ANSWERS_CLASSES} Button {{
    margin-left: 2;
}}
""".strip()
