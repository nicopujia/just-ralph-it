from ...service import JriService as _JriService
from ._registry import main as _main
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

JriService = _JriService
main = _main

_run_approve_draft_promotion = run_approve_draft_promotion
_run_contrast_check = run_contrast_check
_run_delete_task = run_delete_task
_run_edit_draft_task = run_edit_draft_task
_run_edit_readme = run_edit_readme
_run_list_tasks = run_list_tasks
_run_promote_tasks = run_promote_tasks
_run_ralph_result = run_ralph_result
_run_read_readme = run_read_readme
_run_read_tasks = run_read_tasks
_run_rename_task = run_rename_task
_run_upsert_task = run_upsert_task

__all__ = [
    "run_approve_draft_promotion",
    "run_contrast_check",
    "run_delete_task",
    "run_edit_draft_task",
    "run_edit_readme",
    "run_list_tasks",
    "run_promote_tasks",
    "run_ralph_result",
    "run_read_readme",
    "run_read_tasks",
    "run_rename_task",
    "run_upsert_task",
]
