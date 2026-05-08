from pathlib import Path

from ._validation import assert_slug_list, service


def run_promote_tasks(payload: dict[str, object]) -> str:
    slugs = assert_slug_list("slugs", payload.get("slugs")) or []
    check_only = payload.get("check_only", False)
    if not isinstance(check_only, bool):
        raise ValueError("`check_only` must be a boolean")

    jri_service = service(Path.cwd())
    if check_only:
        selected = jri_service.check_draft_promotion(slugs=slugs)
        lines = [f"Promotion check passed for {len(selected)} draft task(s)."]
    else:
        selected = jri_service.promote_drafts(slugs=slugs)
        lines = [f"Promoted {len(selected)} draft task(s) to todo."]
    lines.extend(f"  - {task.slug}" for task in selected)
    return "\n".join(lines)


def run_approve_draft_promotion(payload: dict[str, object]) -> str:
    slugs = assert_slug_list("slugs", payload.get("slugs")) or []
    selected = service(Path.cwd()).approve_draft_promotion(slugs=slugs)
    lines = [f"Approved promotion for {len(selected)} draft task(s)."]
    lines.extend(f"  - {task.slug}" for task in selected)
    return "\n".join(lines)
