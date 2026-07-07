# Implementation Plan

## Confirmed Context

- Canonical notes live at `.jri/notes.yaml`; runtime state stays in `.jri/state.json` and `.jri/interview.json`.
- `src/jri/core/service.py` uses `.jri/notes.yaml`, `.jri/state.json`, and `.jri/interview.json`.
- `src/jri/core/notes.py` persists note mutations to `notes.yaml`, persists focus changes to `state.json`, and removes archived carried note IDs before saving state.
- `src/jri/core/agents/interviewer.py` has the notes tool surface and focus rebuild support.
- Runtime interviewer guidance now uses global notes marked `User control preference:` to adapt question depth and avoid invented stack/architecture assumptions.
- `src/jri/tui/app.py` and `src/jri/tui/widgets/tool_call_row.py` already carry the current UI updates.
- Latest source/spec sweeps found no remaining unimplemented MVP functionality.
- The riskiest path is focus-switch context rebuilding, implemented through `Notes.switch_focus -> Interviewer.after_tool_call/rebuild_context -> Agent.reset_context`.

## Verification

- `uv run --locked ruff format --check src/jri/core src/jri/tui src/jri/cli`
- `uv run --locked ruff check src/jri/core src/jri/tui src/jri/cli`
- `uv run --locked basedpyright src/jri/core src/jri/tui src/jri/cli`
- `./scripts/check.sh`
- bounded `switch_focus` tail smoke passed.
- focused user-control-preference smoke passed.
- focused Ruff format/check and BasedPyright passed.
- `./scripts/check.sh` passed.
- gpt-5.4 verification worker passed Ruff format/check, BasedPyright, and `./scripts/check.sh`.
- Its first `tmux` smoke wrapper failed because zsh treats `status` as readonly; the corrected `tmux` smoke via `bash -lc` passed and rendered the initial prompt.

## Prioritized Remaining Work

- None.

## TL;DR

- The detail/control-preference gap is resolved through runtime interviewer guidance driven by `User control preference:` notes.
- This matters because it prevents invented stack/architecture assumptions and adapts question depth from structured notes.
- `Prioritized Remaining Work` is now `None`.
- bounded `switch_focus` tail smoke passed.
- focused user-control-preference smoke passed.
- focused Ruff format/check and BasedPyright passed.
- `./scripts/check.sh` passed.
- gpt-5.4 verification worker passed Ruff format/check, BasedPyright, and `./scripts/check.sh`.
- The first `tmux` smoke wrapper failed on zsh `status` being readonly; the corrected `bash -lc` smoke passed and rendered the initial prompt.
