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

The opt-in self-hosting proof test can be run with:

```bash
uv run pytest tests/live/test_self_hosting_proof.py --run-self-hosting-proof
```

It demonstrates the full JRI lifecycle — idea to task to loop — against a
repository shaped like this one, using a fake OpenCode client so it runs fast
and deterministically.

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

For promotion review process and `jri reset` behavior, see [ops.md](./ops.md).
