import os
import shutil
import sys

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
INVERSE = "\033[7m"
RESET = "\033[0m"


def supports_color() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if _is_truthy_env(os.environ.get("CLICOLOR_FORCE")):
        return True
    if _is_truthy_env(os.environ.get("FORCE_COLOR")):
        return True
    return sys.stdout.isatty()


def supports_interactive_footer() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def follow_status_bar_clear(*, height: int | None = None) -> str:
    height = max(height or shutil.get_terminal_size((80, 24)).lines, 1)
    return f"\0337\033[{height};1H\033[2K\0338"


def _s(text: str, *codes: str) -> str:
    if not supports_color():
        return text
    return "".join(codes) + text + RESET


def task_header(task_slug: str) -> str:
    label = f" task: {task_slug} "
    width = 60
    if len(label) >= width:
        inner = label
    else:
        dash_count = width - len(label)
        left = dash_count // 2
        right = dash_count - left
        inner = "─" * left + label + "─" * right
    return _s(inner, BOLD, CYAN)


_RESULT_CFG: dict[str, tuple[str, str, str]] = {
    "completed": ("✓ completed", GREEN, ""),
    "incomplete": ("… incomplete", YELLOW, ""),
    "failed": ("✗ failed", RED, ""),
    "needs_human": ("⚠ needs_human", YELLOW, ""),
    "timeout": ("⏱ timeout", RED, ""),
}


def task_footer(result: str) -> str:
    text, color, _ = _RESULT_CFG[result]
    return _s(text, color, BOLD)


def follow_status_bar(
    task_slug: str | None,
    *,
    confirming_halt: bool = False,
    halt_armed: bool = False,
    width: int | None = None,
    height: int | None = None,
) -> str:
    width = max(width or shutil.get_terminal_size((80, 24)).columns, 20)
    height = max(height or shutil.get_terminal_size((80, 24)).lines, 1)
    left = f" task: {task_slug or 'idle'} "
    if confirming_halt:
        right = (
            " halt? Enter confirm  n cancel "
            if halt_armed
            else " halt? y then Enter  n cancel "
        )
    else:
        right = " d detach  s stop-next  h halt "

    if len(left) + len(right) > width:
        left = _truncate(left, max(width - len(right), 1))
    if len(left) + len(right) > width:
        right = _truncate(right, max(width - len(left), 1))

    padding = max(width - len(left) - len(right), 0)
    bar = f"{left}{' ' * padding}{right}"
    styled = _s(bar.ljust(width), BOLD, INVERSE)
    return f"\0337\033[{height};1H\033[2K{styled}\0338"


def trim_tool_output(
    text: str, *, max_lines: int = 20, max_chars: int = 2000
) -> str | None:
    lines = text.splitlines()
    within = len(lines) <= max_lines and len(text) <= max_chars
    if within:
        return None
    if lines and _looks_like_file_list(lines[0]):
        shown = 1
        header = lines[0]
    else:
        shown = min(3, len(lines))
        header = "\n".join(lines[:shown])
    if len(header) > max_chars:
        header = header[: max_chars - 50] + "…"
    trimmed_count = max(len(lines) - shown, 1)
    return _s(f"{header}\n… ({trimmed_count} lines trimmed)", DIM)


def _looks_like_file_list(line: str) -> bool:
    return "/" in line and not line.strip().startswith("#")


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def _is_truthy_env(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() not in {"", "0", "false", "no"}
