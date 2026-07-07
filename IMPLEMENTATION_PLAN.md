# Implementation Plan

## Confirmed Context

- Canonical notes live at `.jri/notes.yaml`; runtime state stays in `.jri/state.json` and `.jri/interview.json`.
- `src/jri/core/service.py` uses `.jri/notes.yaml`, `.jri/state.json`, and `.jri/interview.json`.
- `src/jri/core/notes.py` persists note mutations to `notes.yaml`, persists focus changes to `state.json`, and removes archived carried note IDs before saving state.
- `src/jri/core/agents/interviewer.py` has the notes tool surface and focus rebuild support.
- `src/jri/tui/app.py` and `src/jri/tui/widgets/tool_call_row.py` already carry the current UI updates.
- Latest source/spec sweeps found no remaining unimplemented MVP functionality.
- The riskiest path is focus-switch context rebuilding, implemented through `Notes.switch_focus -> Interviewer.after_tool_call/rebuild_context -> Agent.reset_context`.

## Verification

- `uv run --locked ruff format --check src/jri/core src/jri/tui src/jri/cli`
- `uv run --locked ruff check src/jri/core src/jri/tui src/jri/cli`
- `uv run --locked basedpyright src/jri/core src/jri/tui src/jri/cli`
- `./scripts/check.sh`
- Archived carried IDs are pruned and persisted on reload.
- `read_notes('project', 'all')` excludes `Features`, while `read_notes('all', 'features')` still renders them.
- Temp-dir focus-switch context smoke passed: after switching to feature `f2`, unrelated user context and `f1` notes were absent, while `f2` notes and the current turn tail remained.
- Focused `ruff format`, `ruff check`, `basedpyright`, and `./scripts/check.sh` passed.
- gpt-5.4 subagent ran focused `ruff` and `basedpyright` checks plus a `tmux` TUI startup smoke, saw the initial `What do you want to build?` screen, verified `.jri` files were created in the temp cwd, and cleaned up.

## Prioritized Remaining Work

- None.

## TL;DR

- No remaining unimplemented MVP functionality was found in the latest source/spec sweeps.
- Focus-switch context rebuilding is the main residual risk and is wired through `Notes.switch_focus -> Interviewer.after_tool_call/rebuild_context -> Agent.reset_context`.
- Temp-dir focus-switch context smoke passed with the expected context drop and tail retention.
- Focused `ruff format`, `ruff check`, `basedpyright`, and `./scripts/check.sh` passed.
- gpt-5.4 subagent completed a focused lint/typecheck pass and `tmux` startup smoke with the initial screen and temp-cwd `.jri` artifacts verified.
