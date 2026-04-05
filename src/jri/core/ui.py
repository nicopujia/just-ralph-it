import os
import sys

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def supports_color() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    return sys.stdout.isatty()


def _s(text: str, *codes: str) -> str:
    if not supports_color():
        return text
    return "".join(codes) + text + RESET


def iteration_header(iteration: int, task_slug: str) -> str:
    label = f" iteration {iteration}: {task_slug} "
    width = 60
    if len(label) >= width:
        inner = label
    else:
        dash_count = width - len(label)
        left = dash_count // 2
        right = dash_count - left
        inner = "─" * left + label + "─" * right
    return _s(inner, BOLD, CYAN)


_OUTCOME_CFG: dict[str, tuple[str, str, str]] = {
    "completed": ("✓ completed", GREEN, ""),
    "failed": ("✗ failed", RED, ""),
    "needs human": ("⚠ needs human", YELLOW, ""),
    "timeout": ("⏱ timeout", RED, ""),
}


def iteration_footer(outcome: str) -> str:
    text, color, _ = _OUTCOME_CFG[outcome]
    return _s(text, color, BOLD)


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
