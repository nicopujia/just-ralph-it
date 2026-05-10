import sys
from collections.abc import Callable

from ._validation import load_payload, print_result
from .colors import run_contrast_check
from .graph import (
    run_apply_graph_patch,
    run_compile_graph,
    run_create_node,
    run_move_node,
    run_read_node,
    run_update_node_metadata,
)
from .ralph_result import run_ralph_result
from .readme import run_edit_readme, run_read_readme
from .tasks import (
    run_list_tasks,
    run_read_tasks,
    run_upsert_task,
)

_HANDLERS: dict[str, Callable[[dict[str, object]], str]] = {
    "apply-graph-patch": run_apply_graph_patch,
    "check-contrast": run_contrast_check,
    "compile-graph": run_compile_graph,
    "create-node": run_create_node,
    "edit-readme": run_edit_readme,
    "list-tasks": run_list_tasks,
    "move-node": run_move_node,
    "read-tasks": run_read_tasks,
    "read-node": run_read_node,
    "read-readme": run_read_readme,
    "update-node-metadata": run_update_node_metadata,
    "upsert-task": run_upsert_task,
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
