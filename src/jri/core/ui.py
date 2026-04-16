import os
import sys
from contextlib import ExitStack
from typing import Any

import bottombar

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
PURPLE = "\033[35m"
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


class FollowStatusBar:
    def __init__(self) -> None:
        self._stack = ExitStack()
        self._left_item: Any | None = None
        self._right_item: Any | None = None

    def __enter__(self) -> "FollowStatusBar":
        self._left_item = self._stack.enter_context(bottombar.add(""))
        self._right_item = self._stack.enter_context(bottombar.add("", right=True))
        return self

    def __exit__(self, *args: object) -> None:
        self._stack.close()
        self._left_item = None
        self._right_item = None

    def update(
        self,
        task_slug: str | None,
        *,
        stop_requested: bool = False,
        confirming_halt: bool = False,
        halt_armed: bool = False,
        activity: str | None = None,
        spinner_frame: str | None = None,
    ) -> None:
        left = _follow_status_left_text(
            task_slug,
            activity=activity,
            spinner_frame=spinner_frame,
        )
        right = _follow_status_controls_text(
            stop_requested=stop_requested,
            confirming_halt=confirming_halt,
            halt_armed=halt_armed,
        )
        if self._left_item is not None and self._left_item.text != left:
            self._left_item.text = left
        if self._right_item is not None and self._right_item.text != right:
            self._right_item.text = right


def follow_status_bar(
    task_slug: str | None,
    *,
    stop_requested: bool = False,
    confirming_halt: bool = False,
    halt_armed: bool = False,
    activity: str | None = None,
    spinner_frame: str | None = None,
) -> dict[str, str]:
    return {
        "left": _follow_status_left_text(
            task_slug,
            activity=activity,
            spinner_frame=spinner_frame,
        ),
        "right": _follow_status_controls_text(
            stop_requested=stop_requested,
            confirming_halt=confirming_halt,
            halt_armed=halt_armed,
        ),
    }


def _s(text: str, *codes: str) -> str:
    if not supports_color():
        return text
    return "".join(codes) + text + RESET


def cyan(text: str) -> str:
    return _s(text, CYAN)


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


def _follow_status_left_text(
    task_slug: str | None,
    *,
    activity: str | None = None,
    spinner_frame: str | None = None,
) -> str:
    left = f"task: {task_slug or 'idle'}"
    if activity:
        left += f" {spinner_frame or '|'} {activity}"
    return left


def _follow_status_controls_text(
    *,
    stop_requested: bool = False,
    confirming_halt: bool = False,
    halt_armed: bool = False,
) -> str:
    if confirming_halt:
        return (
            "halt? Enter confirm  n cancel"
            if halt_armed
            else "halt? y then Enter  n cancel"
        )
    if stop_requested:
        return "d detach  s stop (requested)  h halt"
    return "d detach  s stop  h halt"


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


def _is_truthy_env(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() not in {"", "0", "false", "no"}
