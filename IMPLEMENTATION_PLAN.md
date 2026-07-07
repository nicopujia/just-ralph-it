# Implementation Plan

## Confirmed Context

- `IMPLEMENTATION_PLAN.md` was absent in the workspace before this draft.
- `.jri/specs/interviewer-notes.md` requires root-level, git-tracked `notes.yaml`; runtime-only `.jri/state.json`; and existing git-ignored `.jri/interview.json` as a separate transcript.
- `src/jri/core/agents/shared/tool.py` already provides strict `@tool` discovery with Pydantic schemas. Tool parameters must not use Python default values; nullable inputs should be typed as `T | None`.
- `src/jri/core/agents/interviewer.py` currently exposes only `explore`.
- `src/jri/core/service.py` currently creates `.jri`, writes `.jri/.gitignore`, persists full `interviewer.ctx` to `.jri/interview.json`, and restores that full context.
- `src/jri/core/agents/shared/agent.py` currently grows one context list by appending user, model, and tool output items. It has no context rebuild hook.
- `src/jri/tui/app.py` and `src/jri/cli/main.py` are pure chat surfaces with no note or context commands.
- There is no YAML dependency in `pyproject.toml`, no `tests/` directory, and `scripts/check.sh` runs `ruff format`, `ruff check --fix`, and `basedpyright`.

## Prioritized Remaining Work

- **P0: Add the canonical notes/state domain and YAML dependency.**
  - Targets: `pyproject.toml`, `uv.lock`, and a new domain module such as `src/jri/core/notes.py` or `src/jri/core/notes/__init__.py`.
  - Add a YAML parser dependency with `uv add pyyaml`; add type support only if `basedpyright` requires it.
  - Define typed in-memory models for:
    - `project` brief fields: `name`, `tldr`, `goal`, `target_user`, `success_outcome`, `software_type`, `codebase_status`.
    - `global` sections: `requirements`, `constraints`, `questions`, `decisions`.
    - `features`: `id`, `name`, `summary`, and feature-local `requirements`, `constraints`, `questions`, `decisions`.
    - runtime focus state for `.jri/state.json`: `scope`, `feature_id`, `carry_ids`, `reason`.
  - Implement load/save for root `notes.yaml` with stable key ordering and full-file semantic rewrites. Do not patch YAML text directly.
  - Initialize a missing or empty `notes.yaml` with the canonical empty shape, but never put it under `.jri`.
  - Treat malformed notes/state as a clear runtime error instead of silently discarding user project knowledge.

- **P1: Implement semantic note operations before wiring them to LLM tools.**
  - Target: the notes domain module from P0.
  - Implement ID allocation by scanning existing active, resolved, and archived items:
    - features: `f1`, `f2`, ...
    - global requirements/constraints/questions/decisions: `r1`, `c1`, `q1`, `d1`, ...
    - feature-local IDs: `f1/r1`, `f1/c1`, `f1/q1`, `f1/d1`, with counters independent per feature.
  - Implement operations matching the spec: `read_notes`, `set_project_brief`, `add_feature`, `set_feature_brief`, `add_note`, `resolve_question`, `revise_note`, `archive_note`, and `switch_focus` state mutation.
  - Enforce statuses:
    - requirements, constraints, decisions: `active` or `archived`.
    - questions: `open`, `resolved`, or `archived`.
    - resolved questions must link to a decision ID in the same scope.
    - archived notes must store `archive_reason`.
  - Make `read_notes` return compact human-readable summaries only, never raw YAML. Support `scope`, `kind`, `feature_id`, `ids`, and `include_archived` as specified.
  - Return concise tool-result strings that echo both IDs and human text, for example `Added feature f2: saved foods`.
  - Raise `ValueError` for invalid IDs, wrong scopes, unresolved references, or invalid `resolve_question` inputs.

- **P2: Move runtime file ownership into `Service` without deleting tracked project files.**
  - Target: `src/jri/core/service.py`.
  - Create and hold paths for:
    - root `notes.yaml`.
    - `.jri/state.json`.
    - `.jri/interview.json`.
    - `.jri/.gitignore`.
  - Update `.jri/.gitignore` to include exactly runtime files needed for this feature, at minimum `interview.json` and `state.json`; keep `notes.yaml` git-trackable.
  - Change `settings.force` handling so it does not remove tracked `.jri/specs` or root `notes.yaml`. It should clear only runtime files such as `.jri/interview.json` and `.jri/state.json`.
  - Construct the notes runtime before constructing `Interviewer`, then pass it into `Interviewer`.
  - Persist notes after every note mutation and persist state after `switch_focus`.

- **P3: Decouple UI transcript restore from active model context.**
  - Targets: `src/jri/core/service.py` and, if helpful, a small helper module such as `src/jri/core/interview.py`.
  - Keep `.jri/interview.json` as raw chat/tool history for TUI replay only.
  - Stop restoring `.jri/interview.json` directly into `interviewer.ctx`; that would reintroduce the unbounded context the spec is trying to remove.
  - On startup, rebuild the interviewer's active context from `notes.yaml` plus `.jri/state.json`, then separately return interview history rows for the TUI.
  - Preserve existing TUI restore behavior for user, assistant, and tool rows, but derive it from the transcript format rather than active model context.

- **P4: Add the interviewer note tool surface and update the interviewer prompt.**
  - Target: `src/jri/core/agents/interviewer.py`.
  - Add `@tool` methods with the exact spec surface: `read_notes`, `set_project_brief`, `add_feature`, `set_feature_brief`, `add_note`, `resolve_question`, `revise_note`, `archive_note`, and `switch_focus`.
  - Keep `explore`; do not add generic file, patch, diff, path, or YAML-editing tools.
  - Keep signatures strict and default-free, using `Literal[...] | None`, `str | None`, `list[str] | None`, and `bool | None` where the spec allows nullable inputs.
  - Update the system prompt so the interviewer:
    - only reasons in terms of exploration and notes.
    - records durable project facts, requirements, constraints, questions, decisions, and user control/detail preferences.
    - treats missing technical detail as unresolved, not permission to invent stack or architecture.
    - stores delegated implementation authority as a decision boundary rather than inventing a concrete technical decision.
    - infers topic changes and calls `switch_focus` internally without exposing context management to the user.

- **P5: Implement focus switching as a control operation with context rebuild.**
  - Targets: `src/jri/core/agents/shared/agent.py`, `src/jri/core/agents/interviewer.py`, and `src/jri/core/service.py`.
  - Add a small hook or reset method to `Agent` so a subclass can rebuild context after a special tool call without hard-coding notes behavior into the shared framework.
  - Make `switch_focus` update `.jri/state.json` and rebuild active context from:
    - the interviewer system prompt.
    - compact rendered project brief.
    - active global constraints and decisions.
    - active feature brief when focused on a feature.
    - carried note IDs.
    - the current turn and, at most, a small bounded recent tail.
  - Ensure the old full-session context is not carried across focus switches.
  - Include the `switch_focus` function call and output only as needed for the current Responses API tool loop; do not append it to the previous full context.
  - Reuse the same rebuild path during `Service.restore()` so restarts honor the persisted focus state.

- **P6: Preserve pure chat UX in CLI/TUI while hiding internal context mechanics.**
  - Targets: `src/jri/tui/app.py`, `src/jri/tui/widgets/tool_call_row.py`, and `src/jri/cli/main.py` if needed.
  - Do not add user-facing notes commands, context commands, or file-management commands.
  - Avoid showing raw YAML, paths, or `.jri/state.json` details in normal chat.
  - Treat `switch_focus` as internal in the UI. Either suppress its tool row or render it with a non-technical label that does not ask the user to manage focus.
  - For other note tools, prefer a simple non-technical label such as `Updating notes` if raw tool names make the chat feel like a note-management UI.

- **P7: Manual verification and cleanup.**
  - Do not add automated tests unless the user explicitly asks for them.
  - Run `./scripts/check.sh` after implementation.
  - Use `git check-ignore notes.yaml .jri/state.json .jri/interview.json` to confirm `notes.yaml` is not ignored and runtime files are ignored.
  - In a `tmux` window, run `uv run jri` and manually verify a real chat flow:
    - first message creates or updates the project brief in `notes.yaml`.
    - adding two features creates `f1` and `f2` with independent feature-local IDs.
    - unresolved technical details become open questions, not invented decisions.
    - resolving a question links it to a decision and changes question status to `resolved`.
    - changing topics updates `.jri/state.json` and prevents unrelated feature details from remaining in active context.
    - restarting JRI restores visible chat history while rebuilding active context from notes/state.
    - `uv run jri --force` clears runtime state/history without deleting tracked `.jri/specs` or root `notes.yaml`.
  - Per project workflow, have one subagent manually test the implemented flow as a production user would, and another subagent inspect the diff for unnecessary logic/LOC while preserving behavior.

## TL;DR

- Build `notes.yaml` and `.jri/state.json` as first-class domain/runtime state, then expose only semantic note tools to `Interviewer`.
- The main risk is context management: `switch_focus` must rebuild active context from notes/state and must not keep appending to the old full transcript.
- Keep the user experience pure chat, keep `notes.yaml` tracked, keep `.jri/state.json` and `.jri/interview.json` ignored, and verify manually plus `./scripts/check.sh`.
