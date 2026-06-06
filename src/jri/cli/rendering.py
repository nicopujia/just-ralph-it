"""Terminal rendering helpers."""

from jri.core.interview import InterviewQuestion

CYAN = "\x1b[36m"
GRAY = "\x1b[90m"
RESET = "\x1b[0m"
PROMPT = "jri>"
TOOL_PREFIX = "⚙︎"


def render_prompt() -> str:
    """Render the colored input prompt."""
    return f"{CYAN}{PROMPT}{RESET} "


def render_tool_call(tool_name: str) -> str:
    """Render a gray standalone tool-call status line."""
    return f"{GRAY}{TOOL_PREFIX} {tool_name}{RESET}\n"


def render_question(question: InterviewQuestion) -> str:
    """Render an interviewer question for the next REPL turn."""
    rendered = [
        f"{question.level.title()}-level question:",
        question.question,
    ]
    if question.choices:
        rendered.extend(["", "Options:"])
        for index, choice in enumerate(question.choices):
            prefix = chr(ord("A") + index)
            suffix = " (default)" if choice.label == question.default else ""
            description = (
                f" - {choice.description}" if choice.description else ""
            )
            rendered.append(f"{prefix}. {choice.label}{suffix}{description}")
    return "\n".join(rendered)
