# Contributing

## Setup

### Prerequisites

Mandatory:

- [Git](https://git-scm.com/install/)
- `make`
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [OpenCode](https://opencode.ai/docs/#install)

Recommended:

- VPS or [Docker](https://docs.docker.com/get-started/get-docker/).
  VPS is preferred if you want to give as much power as possible to Ralph.

### Development

Set up the local environment:

```bash
uv sync
uv run pre-commit install
uv run pre-commit install --hook-type pre-push
uv run jri --help
```

Or install system-wide (editable):

```bash
uv tool install -e .
uv tool update-shell # if 'jri' not found, restart shell
jri --help

uv tool install -e --force .    # reinstall after dependency changes
uv tool uninstall jri           # remove later
```

Source changes under `src/jri/` are picked up immediately with the editable install.

Run the canonical pre-merge validation suite with:

```bash
make check
```

`make check` runs the repo quality gates for linting, formatting, typing, schema validation, and tests.
Git hooks remain useful guardrails (see [pre-commit config](../.pre-commit-config.yaml)), but `make check` is the canonical repo-wide validation command.

The opt-in live OpenCode test can be run with:

```bash
uv run pytest tests/live/test_live_opencode.py --run-live-opencode --opencode-model opencode/qwen3.6-plus-free
```

You can specify any available model you want.
I recommend using the [free models from OpenCode Zen](https://opencode.ai/docs/zen/#pricing).

### Build

Build distribution artifacts with:

```bash
uv build
```

This produces a wheel and source distribution under `dist/`.

## Guidelines

- Use `uv` for anything related to Python
- Use mostly lowercase for commit messages, abbrs. encouraged
- Code update = corresponding docs update
- Follow TDD principles when writing code
- Maintain docs concise
- Split markdown lines at sentence boundaries

Task metadata guidance:
Draft tasks may omit `acceptance_criteria` while the work is still being clarified.
Tasks promoted to `todo`, `doing`, or `done` must include a non-empty `acceptance_criteria` list.

## `jri reset` contract

`jri reset` is a recovery operation that restores the repo to the last successful iteration.
It works even when the working tree is dirty or a feature branch is checked out.

### What is restored

- The default branch is hard-reset to the `jri/<iteration_number>` tag.

### What is discarded

- All commits on the default branch after that tag.
- Uncommitted changes to tracked files.
- Any leftover `ralph/*` feature branches from failed or stale runs.
- In-progress runtime state (`process`, `active_attempt`, `started_at` in `state.json`).

### What is preserved

- `iteration_number`, `finished_at`, `session`, and `branch` in `state.json`.
- The full attempt history for diagnostics.

### Preconditions

- The project must be initialized (`jri init`).
- At least one successful iteration must exist (`iteration_number >= 1`).

### Postconditions

- The default branch is checked out and points to `jri/<iteration_number>`.
- The working tree matches the tag (tracked files are clean).
- `state.json` has no `process`, `active_attempt`, or `started_at`.
- Attempt history is preserved for diagnostics.
