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

Set up the local environment with:

```bash
uv sync
uv run pre-commit install
```

During development, run the CLI from the repo with:

```bash
uv run jri --help
```

To run `jri` from anywhere on the system, install this repo as an editable uv
tool from the repo root:

```bash
uv tool install -e .
```

Then you can use:

```bash
jri --help
```

If your shell still cannot find `jri`, run:

```bash
uv tool update-shell
```

and restart the shell.

With the editable tool install, source changes under `src/jri/` are picked up immediately.
If you change dependencies or packaging metadata, reinstall the tool:

```bash
uv tool install -e --force .
```

To remove the system-wide command later:

```bash
uv tool uninstall jri
```

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
- Commit and push frequently
- Maintain docs concise
- Split markdown lines at sentence boundaries
