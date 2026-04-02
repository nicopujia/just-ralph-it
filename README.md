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
jri --help
```

### Runtime configuration

Prefer CLI arguments for per-run behavior.
`jri` can also read fallback configuration from the process environment, but it
does not load `.env` files automatically.

Preferred CLI option:

- `jri start --model <model>`: choose the OpenCode model for that Ralph run

Fallback environment variable:

- `JRI_OPENCODE_MODEL`: optional model override for Ralph runs started via
  `jri start` when `--model` is not provided

Example:

```bash
uv run jri start --model opencode/qwen3.6-plus-free
```

Agent definitions are written to `.opencode/agents/` during `jri init`.
Their source templates live in `src/jri/prompts.py`. The dynamic per-task
Ralph prompt is assembled in `src/jri/core/service.py`.

### Guidelines

- Use `uv` for anything related to Python
- Use mostly lowercase for commit messages, abbrs. encouraged
- Code update = corresponding docs update
- Follow TDD principles when writing code
- Commit and push frequently
- Maintain docs concise
