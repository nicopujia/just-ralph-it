from rich.text import Text

PRIMARY_STYLE = "yellow"
MUTED_STYLE = "bright_black"
ERROR_STYLE = "red"

REPL_PROMPT: Text = Text("jri> ", style=PRIMARY_STYLE)
EXIT_MESSAGE: Text = Text("Bye!", style=MUTED_STYLE)
CONFIG_ERROR_MESSAGE: Text = Text("Invalid configuration.", style=ERROR_STYLE)
CONFIG_ERROR_HELP_MESSAGE_TEMPLATE: str = """
Set or fix these environment variables:

%s

You can define them in your shell or in a .env file on this directory.
"""
