import sys
from collections.abc import Callable
from typing import Any

from .contrast import _run_contrast_check
from .promotion import _run_approve_draft_promotion, _run_promote_tasks
from .ralph_result import _run_ralph_result
from .readme import _run_edit_readme, _run_read_readme
from .tasks import (
    _run_delete_task,
    _run_edit_draft_task,
    _run_list_tasks,
    _run_read_tasks,
    _run_rename_task,
    _run_upsert_task,
)
from .validation import _load_payload, _print_result

_HANDLERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "check-contrast": _run_contrast_check,
    "edit-draft-task": _run_edit_draft_task,
    "edit-readme": _run_edit_readme,
    "list-tasks": _run_list_tasks,
    "read-tasks": _run_read_tasks,
    "read-readme": _run_read_readme,
    "upsert-task": _run_upsert_task,
    "rename-task": _run_rename_task,
    "delete-task": _run_delete_task,
    "approve-draft-promotion": _run_approve_draft_promotion,
    "promote-tasks": _run_promote_tasks,
    "ralph-result": _run_ralph_result,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in _HANDLERS:
        available = ", ".join(sorted(_HANDLERS))
        print(f"expected one tool name ({available})", file=sys.stderr)
        return 2

    try:
        payload = _load_payload()
        _print_result(_HANDLERS[argv[0]](payload))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
