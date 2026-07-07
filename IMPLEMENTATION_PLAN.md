# Implementation Plan

## Confirmed Context

- `.jri/specs/interviewer-notes.md` still defines the target shape: root-level `notes.yaml`, runtime-only `.jri/state.json`, and git-ignored `.jri/interview.json`.
- `src/jri/core/notes.py` now exists with typed models and semantic note operations.
- `pyproject.toml` now includes `PyYAML`.
- `src/jri/core/service.py` now creates the `notes.yaml` and `.jri/state.json` paths, passes `Notes` into `Interviewer`, persists `.jri/interview.json` as visible transcript only, and no longer reloads it into `interviewer.ctx`.
- `.jri/.gitignore` now includes `interview.json` and `state.json`.
- `--force` now clears only runtime interview/state files and leaves `.jri/specs` and root `notes.yaml` intact.
- `src/jri/core/notes.py` saves `notes.yaml` after every semantic note mutation and saves `.jri/state.json` after `switch_focus`.
- `src/jri/core/agents/shared/tool.py` still provides strict `@tool` discovery with Pydantic schemas. Tool parameters must not use Python default values; nullable inputs should be typed as `T | None`.
- `src/jri/core/agents/interviewer.py` now has the full notes tool surface, a notes-aware system prompt, and `switch_focus`-driven context rebuild support.
- `src/jri/core/agents/shared/agent.py` now has a reset hook plus an `after_tool_call` hook so subclasses can rebuild context after control operations.
- `src/jri/tui/app.py` and `src/jri/tui/widgets/tool_call_row.py` now use non-technical tool labels and the restore placeholder no longer stays on the initial empty-state copy after history exists.
- `scripts/check.sh` still runs `ruff format`, `ruff check --fix`, and `basedpyright`.
- Verification passed: `./scripts/check.sh`; smoke checks for tool schema, note mutations, focus rebuild, and transcript restore; `git check-ignore` confirmed `.jri/state.json` and `.jri/interview.json` are ignored while `notes.yaml` is not; independent `tmux` TUI smoke passed.

## Prioritized Remaining Work

No remaining implementation follow-ups are identified for this increment.

## TL;DR

- The notes handoff, context rebuild, visible-transcript restore, and UI labeling work are done and verified.
- There are no remaining follow-up items from this increment.
