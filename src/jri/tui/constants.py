# Maintain constants ordered alphabetically with one blank line between
# different initial letter.

CONFIG_ERROR_COPY: str = """Invalid configuration.
Set or fix these environment variables:

%s

You can define them in your shell or in a .env file on this directory.
"""

INTERIVEWER_ERROR_COPY = """
Something went wrong while talking to the interviewer:

%s

Please try again or contact Nico.
"""
INTERVIEWER_MESSAGE_CLASSES: str = "interviewer-message"
INTERVIEWER_NO_RESPONSE_COPY = "_No response received._"
INTERVIEWER_THINKING_COPY = "_Thinking..._"

MESSAGES_CONTAINER_ID: str = "messages"
MESSAGE_INPUT_ID: str = "message-input"
MESSAGE_INPUT_PLACEHOLDER_COPY = "What do you want to build?"

TITLE_COPY = "Just Ralph It"

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
    padding-top: 1;
    padding-bottom: 1;
    padding-right: 2;
    padding-left: 2;
}}

#{MESSAGE_INPUT_ID} {{
    margin-top: 0;
    margin-right: 2;
    margin-bottom: 1;
    margin-left: 2;
}}

.{USER_MESSAGE_CLASSES} {{
    margin-top: 1;
    text-style: bold;
}}

.{INTERVIEWER_MESSAGE_CLASSES} {{
    margin-top: 1;
    margin-left: 1;
}}
"""
