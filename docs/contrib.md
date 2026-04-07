# Contributing

## Setup

```bash
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

# [Optional] E2E test
uv run pytest tests/live/test_live_opencode.py --run-live-opencode --opencode-model opencode/qwen3.6-plus-free
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
