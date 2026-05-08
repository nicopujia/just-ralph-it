import sys
from collections.abc import Callable

from ._validation import load_payload, print_result
from .colors import run_contrast_check
from .promotion import run_approve_draft_promotion, run_promote_tasks
from .ralph_result import run_ralph_result
from .readme import run_edit_readme, run_read_readme
from .tasks import (
    run_delete_task,
    run_edit_draft_task,
    run_list_tasks,
    run_read_tasks,
    run_rename_task,
    run_upsert_task,
)

_HANDLERS: dict[str, Callable[[dict[str, object]], str]] = {
    "check-contrast": run_contrast_check,
    "edit-draft-task": run_edit_draft_task,
    "edit-readme": run_edit_readme,
    "list-tasks": run_list_tasks,
    "read-tasks": run_read_tasks,
    "read-readme": run_read_readme,
    "upsert-task": run_upsert_task,
    "rename-task": run_rename_task,
    "delete-task": run_delete_task,
    "approve-draft-promotion": run_approve_draft_promotion,
    "promote-tasks": run_promote_tasks,
    "ralph-result": run_ralph_result,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in _HANDLERS:
        available = ", ".join(sorted(_HANDLERS))
        print(f"expected one tool name ({available})", file=sys.stderr)
        return 2

    try:
        payload = load_payload()
        print_result(_HANDLERS[argv[0]](payload))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
