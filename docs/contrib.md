# Contributing

## Setup

```bash
# Install dependencies and Git hooks
make setup
```

## Validate

```bash
# Verify things work
make check

# Measure test coverage
make coverage

# [Optional] Live integration tests
uv run pytest tests/live --run-live-agent --preset openai
```

When `--run-live-agent` is set, pytest capture is disabled so live agent stdout
streams directly to the terminal while the test is running.

## Build

```bash
# Produce wheel + sdist under dist/
uv build
```

## Guidelines

- Use `uv` for Python tooling.
- Keep CLI docs in the CLI help text (`--help`), not in markdown files.
- Lowercase commit messages, abbreviations encouraged.
- Code changes require corresponding docs updates if docs reference that part of the code.
- Follow TDD principles.
- Keep docs concise, split lines at sentence boundaries.
