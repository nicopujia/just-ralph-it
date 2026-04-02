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

The Python source lives under `src/jri/`.

For usage, architecture, and development workflow, see [docs/](./docs).

### Tests

Run the normal suite with:

```bash
uv run pytest
```

### Guidelines

- Use `uv` for anything related to Python
- Use mostly lowercase for commit messages, abbrs. encouraged
- Code update = corresponding docs update
- Follow TDD principles when writing code
- Commit and push frequently
- Maintain docs concise
