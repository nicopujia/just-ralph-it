# Contributing

## Setup

### Prerequisites

Mandatory:

- [Git](https://git-scm.com/install/)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [OpenCode](https://opencode.ai/docs/#install)

Recommended:

- VPS or [Docker](https://docs.docker.com/get-started/get-docker/). VPS is preferred if you want to give as much power as possible to Ralph.

### Development

Set up the local environment with:

```bash
uv sync
uv run pre-commit install
```

During development, run the CLI from the repo with:

```bash
jri --help
```

The project is installed in editable mode, so Python source changes under `src/jri/` are picked up immediately. 
If you change dependencies or packaging metadata, run `uv sync` again.

Typical checks (linter, formatter, and testing) are already enforced via Git hooks.
The opt-in live OpenCode test can be run with:

```bash
uv run pytest tests/live/test_live_opencode.py --run-live-opencode --opencode-model opencode/qwen3.6-plus-free
```

You can specify any available model you want. I recommend using the [free models from OpenCode Zen](https://opencode.ai/docs/zen/#pricing).

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
