"""Pydantic AI tool adapters for the interviewer agent."""

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from time import perf_counter
from typing import Annotated, Literal, Protocol

from pydantic import Field
from pydantic_ai import ModelRetry, RunContext, Tool

from jri.core.logging import JsonlLogger, JsonValue
from jri.core.readiness import (
    check_mvp_readiness,
    format_missing_mvp_readiness,
)
from jri.core.tools.ask import AskChoice, build_question
from jri.core.tools.explore import ContextExplorer, explore_context
from jri.core.tools.just_ralph_it import JustRalphItError, finalize_jri
from jri.core.tools.note import replace_note, write_note
from jri.core.tools.spec import (
    SpecValidationError,
    replace_spec,
    validate_spec_markdown,
    write_spec,
)
from jri.core.tools.write import WriteError, parse_patch
from jri.core.triggers import is_trigger_message

SpecPath = Annotated[
    str,
    Field(
        description=(
            "Relative Markdown filename under specs, without absolute "
            "directories. Use values like product or product.md."
        ),
    ),
]
SpecName = Annotated[
    str,
    Field(description="Name of the spec to update, such as product."),
]
SpecContent = Annotated[
    str,
    Field(description="Complete confirmed Markdown content for this spec."),
]
NotesContent = Annotated[
    str,
    Field(
        description=(
            "Markdown notes for unresolved branches, pending questions, "
            "assumptions, or decisions."
        ),
    ),
]
ContextRequest = Annotated[
    str,
    Field(
        description=(
            "Specific context to gather before deciding what to ask or record."
        ),
    ),
]
QuestionLevel = Annotated[
    Literal["high", "low"],
    Field(
        description=(
            "Question scope: high for broad product direction, low for "
            "specific behavior choices."
        ),
    ),
]
QuestionText = Annotated[
    str,
    Field(description="Question to ask the user next."),
]
DefaultChoice = Annotated[
    str | None,
    Field(description="Optional default choice label to preselect."),
]
ReadinessSummary = Annotated[
    str,
    Field(description="Why the confirmed specs are ready to finalize."),
]
FinalSpecContent = Annotated[
    str,
    Field(description="Complete final Markdown spec content to save."),
]
KnownBlockers = Annotated[
    list[str] | None,
    Field(description="Missing decisions that prevent finalization."),
]
PatchText = Annotated[
    str,
    Field(
        description=(
            "Complete patch envelope. Use *** Begin Patch and *** End Patch. "
            "For Add File, every body line must start with +, including blank "
            "Markdown lines written as +."
        ),
    ),
]


class InterviewerToolDeps(Protocol):
    """Dependencies required by interviewer model tools."""

    project_root: Path
    latest_user_message: str
    logger: JsonlLogger
    explorer: ContextExplorer
    finalized: bool


async def write_spec_tool(
    ctx: RunContext[InterviewerToolDeps],
    path: SpecPath,
    patch_text: PatchText,
) -> str:
    """Patch one curated project spec file with a structured patch."""
    return await run_logged_tool(
        ctx.deps,
        "spec",
        {"path": path, "patch_text": patch_text},
        lambda: _run_patch_tool(
            lambda: write_spec(
                project_root=ctx.deps.project_root,
                path=path,
                patch_text=patch_text,
            ),
            patch_text=patch_text,
        ),
    )


async def write_note_tool(
    ctx: RunContext[InterviewerToolDeps],
    patch_text: PatchText,
) -> str:
    """Patch the interviewer scratchpad with a structured patch."""
    return await run_logged_tool(
        ctx.deps,
        "note",
        {"patch_text": patch_text},
        lambda: _run_patch_tool(
            lambda: write_note(
                project_root=ctx.deps.project_root,
                patch_text=patch_text,
            ),
            patch_text=patch_text,
        ),
    )


async def update_specs_tool(
    ctx: RunContext[InterviewerToolDeps],
    spec_name: SpecName,
    content: SpecContent,
) -> str:
    """Create or replace a confirmed project spec by name."""
    return await run_logged_tool(
        ctx.deps,
        "update_specs",
        {"spec_name": spec_name, "content": content},
        lambda: _replace_validated_spec(ctx.deps, spec_name, content),
    )


async def update_scratchpad_tool(
    ctx: RunContext[InterviewerToolDeps],
    notes: NotesContent,
) -> str:
    """Record concise interview memory for unresolved context."""
    return await _update_scratchpad_with_tool_name(
        ctx,
        notes=notes,
        tool_name="update_scratchpad",
    )


async def record_notes_tool(
    ctx: RunContext[InterviewerToolDeps],
    notes: NotesContent,
) -> str:
    """Record interview notes for callers using the older tool name."""
    return await _update_scratchpad_with_tool_name(
        ctx,
        notes=notes,
        tool_name="record_notes",
    )


def build_interviewer_tools() -> list[Tool[InterviewerToolDeps]]:
    """Build the strict tool set exposed to the interviewer model."""
    return [
        Tool(
            ask_question_tool,
            takes_ctx=False,
            name="ask_question",
            strict=True,
        ),
        Tool(
            update_scratchpad_tool,
            takes_ctx=True,
            name="update_scratchpad",
            max_retries=3,
            strict=True,
        ),
        Tool(
            update_specs_tool,
            takes_ctx=True,
            name="update_specs",
            max_retries=3,
            strict=True,
        ),
        Tool(
            explore_context_tool,
            takes_ctx=True,
            name="explore_context",
            strict=True,
        ),
        Tool(
            finalize_specs_tool,
            takes_ctx=True,
            name="finalize_specs",
            max_retries=3,
            strict=True,
        ),
    ]


def ask_question_tool(
    level: QuestionLevel,
    question: QuestionText,
    choices: list[AskChoice] | None = None,
    default: DefaultChoice = None,
) -> str:
    """Ask the next high-leverage question for the user to answer."""
    _ = build_question(
        level=level, question=question, choices=choices, default=default
    )
    return "Question recorded for the next user turn."


async def explore_context_tool(
    ctx: RunContext[InterviewerToolDeps],
    request: ContextRequest,
) -> str:
    """Explore read-only context before deciding."""
    return await run_logged_tool(
        ctx.deps,
        "explore_context",
        {"request": request},
        lambda: explore_context(
            project_root=ctx.deps.project_root,
            request=request,
            explorer=ctx.deps.explorer,
        ),
    )


async def finalize_specs_tool(
    ctx: RunContext[InterviewerToolDeps],
    readiness_summary: ReadinessSummary,
    spec_content: FinalSpecContent,
    spec_name: SpecName = "product",
    known_blockers: KnownBlockers = None,
) -> str:
    """Finalize saved specs and finish the interview."""
    blocker_log: list[JsonValue] = list(known_blockers or [])

    async def finalize() -> str:
        if not spec_content.strip():
            msg = "finalize_specs requires non-empty final spec_content."
            raise ModelRetry(msg)
        if not is_trigger_message(ctx.deps.latest_user_message):
            raise ModelRetry(
                _format_finalize_retry_message(
                    "finalize_specs requires a trigger phrase."
                )
            )
        if known_blockers:
            return _format_finalize_blocked_message(known_blockers)
        readiness = check_mvp_readiness(spec_content)
        if not readiness.is_ready:
            return format_missing_mvp_readiness(readiness.missing)
        try:
            await replace_spec(
                project_root=ctx.deps.project_root,
                path=spec_name,
                content=spec_content,
            )
        except WriteError as exc:
            raise ModelRetry(_format_spec_name_retry_message()) from exc
        try:
            result = await finalize_jri(
                project_root=ctx.deps.project_root,
                latest_user_message=ctx.deps.latest_user_message,
                readiness_summary=readiness_summary,
                known_blockers=known_blockers,
            )
        except JustRalphItError as exc:
            raise ModelRetry(_format_finalize_retry_message(str(exc))) from exc
        ctx.deps.finalized = result.should_exit
        return result.message

    return await run_logged_tool(
        ctx.deps,
        "finalize_specs",
        {"known_blockers": blocker_log, "spec_name": spec_name},
        finalize,
    )


async def run_logged_tool(
    deps: InterviewerToolDeps,
    tool_name: str,
    arguments: Mapping[str, JsonValue],
    action: Callable[[], Awaitable[str]],
) -> str:
    """Run an interviewer tool action with structured logging."""
    started_at = perf_counter()
    deps.logger.write(
        "tool_call_started",
        {"tool_name": tool_name, "arguments": dict(arguments)},
    )
    try:
        result = await action()
    except Exception as exc:
        deps.logger.write(
            "tool_call_failed",
            {
                "tool_name": tool_name,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "duration_ms": round((perf_counter() - started_at) * 1000, 3),
            },
        )
        raise
    deps.logger.write(
        "tool_call_finished",
        {
            "tool_name": tool_name,
            "result": result,
            "duration_ms": round((perf_counter() - started_at) * 1000, 3),
        },
    )
    return result


async def _replace_validated_spec(
    deps: InterviewerToolDeps,
    spec_name: str,
    content: str,
) -> str:
    try:
        validate_spec_markdown(content)
    except SpecValidationError as exc:
        raise ModelRetry(
            _format_spec_markdown_retry_message(str(exc))
        ) from exc
    try:
        return await replace_spec(
            project_root=deps.project_root,
            path=spec_name,
            content=content,
        )
    except WriteError as exc:
        raise ModelRetry(_format_spec_name_retry_message()) from exc


async def _update_scratchpad_with_tool_name(
    ctx: RunContext[InterviewerToolDeps],
    *,
    notes: str,
    tool_name: str,
) -> str:
    return await run_logged_tool(
        ctx.deps,
        tool_name,
        {"notes": notes},
        lambda: _record_merged_notes(ctx.deps, notes),
    )


async def _record_merged_notes(
    deps: InterviewerToolDeps,
    notes: str,
) -> str:
    scratchpad = deps.project_root / ".jri" / "scratchpad.md"
    existing = (
        scratchpad.read_text(encoding="utf-8") if scratchpad.exists() else ""
    )
    return await replace_note(
        project_root=deps.project_root,
        content=_merge_notes(existing, notes),
    )


def _merge_notes(existing: str, notes: str) -> str:
    current = existing.strip()
    incoming = notes.strip()
    if not current:
        return f"{incoming}\n" if incoming else ""
    if not incoming or incoming in current:
        return f"{current}\n"
    return f"{current}\n\n{incoming}\n"


def _validate_patch_text(patch_text: str) -> None:
    if not patch_text.strip():
        raise ModelRetry(_format_patch_retry_message("patch_text is empty"))
    try:
        hunks = parse_patch(patch_text)
    except ValueError as exc:
        raise ModelRetry(_format_patch_retry_message(str(exc))) from exc
    if not hunks:
        raise ModelRetry(
            _format_patch_retry_message("patch_text has no file hunks")
        )


async def _run_patch_tool(
    action: Callable[[], Awaitable[str]],
    *,
    patch_text: str,
) -> str:
    try:
        _validate_patch_text(patch_text)
        return await action()
    except WriteError as exc:
        raise ModelRetry(_format_patch_retry_message(str(exc))) from exc


def _format_patch_retry_message(reason: str) -> str:
    return (
        f"Invalid patch_text: {reason}. Send patch_text as a complete patch "
        "envelope. For Add File, every content line must start with +, and "
        "blank Markdown lines must be written as +. Also pass spec paths as "
        "relative names like product, never absolute paths. Example:\n"
        "*** Begin Patch\n"
        "*** Add File: product.md\n"
        "+# Product\n"
        "+\n"
        "+First paragraph.\n"
        "*** End Patch"
    )


def _format_spec_markdown_retry_message(reason: str) -> str:
    return (
        f"Invalid update_specs content: {reason}. Send spec-shaped Markdown "
        "with headings and confirmed requirement text, not raw prose, code, "
        "or an implementation answer."
    )


def _format_spec_name_retry_message() -> str:
    return (
        "Invalid spec_name. Use a spec name such as product or product.md; "
        "it must be relative, never an absolute path or a name with .. "
        "segments."
    )


def _format_finalize_blocked_message(blockers: list[str]) -> str:
    lines = ["Cannot finalize specs yet:"]
    lines.extend(f"- {blocker}" for blocker in blockers)
    return "\n".join(lines)


def _format_finalize_retry_message(reason: str) -> str:
    return (
        f"Invalid finalize_specs call: {reason} Call finalize_specs only when "
        "the latest user message is the trigger phrase, and pass known "
        "blockers instead of finalizing when the spec is not ready."
    )
