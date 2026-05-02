from pathlib import Path
from typing import Any

from .validation import _assert_slug_list, _service


def _run_promote_tasks(payload: dict[str, Any]) -> str:
    slugs = _assert_slug_list("slugs", payload.get("slugs")) or []
    check_only = payload.get("check_only", False)
    if not isinstance(check_only, bool):
        raise ValueError("`check_only` must be a boolean")

    service = _service(Path.cwd())
    if check_only:
        selected = service.check_draft_promotion(slugs=slugs)
        lines = [f"Promotion check passed for {len(selected)} draft task(s)."]
    else:
        selected = service.promote_drafts(slugs=slugs)
        lines = [f"Promoted {len(selected)} draft task(s) to todo."]
    lines.extend(f"  - {task.slug}" for task in selected)
    return "\n".join(lines)


def _run_approve_draft_promotion(payload: dict[str, Any]) -> str:
    slugs = _assert_slug_list("slugs", payload.get("slugs")) or []
    selected = _service(Path.cwd()).approve_draft_promotion(slugs=slugs)
    lines = [f"Approved promotion for {len(selected)} draft task(s)."]
    lines.extend(f"  - {task.slug}" for task in selected)
    return "\n".join(lines)
