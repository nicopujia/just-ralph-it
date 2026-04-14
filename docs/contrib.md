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

# [Optional] Live integration tests
uv run pytest tests/live -L -M vercel/alibaba/qwen3.6-plus
```

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
