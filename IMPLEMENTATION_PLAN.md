# Implementation Plan

## Confirmed Context

- `.jri/specs/interviewer-notes.md` still defines the target shape: root-level `notes.yaml`, runtime-only `.jri/state.json`, and git-ignored `.jri/interview.json`.
- `src/jri/core/notes.py` now exists with typed models and semantic note operations.
- `pyproject.toml` now includes `PyYAML`.
- `src/jri/core/service.py` now creates the `notes.yaml` and `.jri/state.json` paths.
- `.jri/.gitignore` now includes `interview.json` and `state.json`.
- `--force` now clears only runtime interview/state files and leaves `.jri/specs` and root `notes.yaml` intact.
- `src/jri/core/notes.py` saves `notes.yaml` after every semantic note mutation and saves `.jri/state.json` after `switch_focus`.
- `src/jri/core/agents/shared/tool.py` still provides strict `@tool` discovery with Pydantic schemas. Tool parameters must not use Python default values; nullable inputs should be typed as `T | None`.
- `src/jri/core/agents/interviewer.py` still exposes only `explore`.
- `src/jri/core/service.py` still restores the old full interview context; notes/state rebuild is not wired yet.
- `src/jri/core/agents/shared/agent.py` still grows one context list by appending user, model, and tool output items. It has no context rebuild hook.
- `src/jri/tui/app.py` and `src/jri/cli/main.py` are still pure chat surfaces with no note or context commands.
- `scripts/check.sh` still runs `ruff format`, `ruff check --fix`, and `basedpyright`.

## Prioritized Remaining Work

- **P2: Finish the notes runtime handoff from `Service` to `Interviewer`.**
  - Target: `src/jri/core/service.py` and `src/jri/core/agents/interviewer.py`.
  - Pass the existing `Service.notes` runtime into `Interviewer` when adding note tools.
  - Keep `Service` as the owner of root `notes.yaml`, `.jri/state.json`, `.jri/interview.json`, and `.jri/.gitignore`.
  - Do not reintroduce broad `.jri` deletion for `settings.force`; it must keep preserving `.jri/specs` and root `notes.yaml`.

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

- The canonical notes domain, YAML support, and semantic note operations are done.
- The remaining work is the service/tool wiring and context rebuild path: `Interviewer` still only has `explore`, and `switch_focus` / notes-state reconstruction are not wired yet.
- Keep the user experience pure chat, keep `notes.yaml` tracked, keep `.jri/state.json` and `.jri/interview.json` ignored, and verify manually plus `./scripts/check.sh`.
