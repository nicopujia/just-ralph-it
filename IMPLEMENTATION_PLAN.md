# Implementation Plan

## Confirmed Context

- Canonical notes live at `.jri/notes.yaml`; runtime state stays in `.jri/state.json` and `.jri/interview.json`.
- `src/jri/core/service.py` uses `.jri/notes.yaml`, `.jri/state.json`, and `.jri/interview.json`.
- `src/jri/core/notes.py` persists note mutations to `notes.yaml`, persists focus changes to `state.json`, and removes archived carried note IDs before saving state.
- `src/jri/core/agents/interviewer.py` has the notes tool surface and focus rebuild support.
- `src/jri/tui/app.py` and `src/jri/tui/widgets/tool_call_row.py` already carry the current UI updates.

## Verification

- `uv run --locked ruff format --check src/jri/core src/jri/tui src/jri/cli`
- `uv run --locked ruff check src/jri/core src/jri/tui src/jri/cli`
- `uv run --locked basedpyright src/jri/core src/jri/tui src/jri/cli`
- Archived carried IDs are pruned and persisted on reload.
- `read_notes('project', 'all')` excludes `Features`, while `read_notes('all', 'features')` still renders them.
- Focused `ruff format`, `ruff check`, and `basedpyright` passed.

## Prioritized Remaining Work

- None.

## TL;DR

- `.jri/notes.yaml` path alignment and the archived carried-note reload fix are resolved.
- Focused lint, typecheck, temp-dir smoke, tmux TUI startup smoke, and `./scripts/check.sh` all passed.
