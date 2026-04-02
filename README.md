# Just Ralph It

> The proper tool around the Ralph Wiggum technique.

## Resources

- [Docs](./docs)
- [Concept](https://nicolaspujia.com/ralph)
- [Original article about the Ralph Wiggum technique, by Geoffrey Huntley](https://ghuntley.com/ralph)

## Contributing

### Prerequisites

Mandatory:

- [Git](https://git-scm.com/install/)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [OpenCode](https://opencode.ai/docs/#install)

Recommended:

- VPS or [Docker](https://docs.docker.com/get-started/get-docker/). VPS is preferred if you want to give as much power as possible to Ralph.

### Setup

```bash
uv sync
uv run pre-commit install
uv run jri --help
```

This repo is an installable Python package named `jri`.
Its runtime source lives under `src/jri/`, and you can run it from the repo
with either `uv run jri ...` or `uv run python -m jri ...`.

### Runtime configuration

Use CLI arguments for runtime behavior.

- `jri start --model <model>`: choose the OpenCode model for that Ralph run

Example:

```bash
uv run jri start --model opencode/qwen3.6-plus-free
```

Agent definitions are written to `.opencode/agents/` during `jri init`.
Their source templates live in `src/jri/prompts.py`. The dynamic per-task
Ralph prompt is assembled in `src/jri/core/service.py`.

### Tests

Run the normal suite with:

```bash
uv run pytest
```

The live OpenCode test is opt-in and controlled with pytest options rather than
environment variables:

```bash
uv run pytest tests/live/test_live_opencode.py --run-live-opencode --opencode-model opencode/qwen3.6-plus-free
```

### Guidelines

- Use `uv` for anything related to Python
- Use mostly lowercase for commit messages, abbrs. encouraged
- Code update = corresponding docs update
- Follow TDD principles when writing code
- Commit and push frequently
- Maintain docs concise
