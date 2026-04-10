# Contributing

## Setup

```bash
# Fetch skill repos
git submodule update --init --recursive

# Install Python dependencies
uv sync

# Install Git hooks
uv run pre-commit install
uv run pre-commit install --hook-type pre-push
```

## Validate

```bash
# Verify things work
make check

# [Optional] Live integration tests
uv run pytest tests/live -L -M opencode/minimax-m2.5
```

## Build

```bash
# Produce wheel + sdist under dist/
uv build
```

## Guidelines

- Use `uv` for Python tooling.
- Lowercase commit messages, abbreviations encouraged.
- Code changes require corresponding docs updates if docs reference that part of the code.
- Follow TDD principles.
- Keep docs concise, split lines at sentence boundaries.
