# Contributing

## Setup

### Prerequisites

Mandatory:

- [Git](https://git-scm.com/install/)
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

Typical checks (linter, formatter, and testing) are already enforced via Git hooks (see [pre-commit config](../.pre-commit-config.yaml)).
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
