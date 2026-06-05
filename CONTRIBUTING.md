# Contributing

```bash
# Install all dependencies, including development ones
uv sync --all-groups

# Run CLI inside this repo
uv run jri --help

# Validate changes
# Always run this command after making changes
./scripts/validate.py
```

## Guidelines

- Follow strict test-driven development (TDD), maintaining 100% coverage of [source code](./src/) at all times. A good resource on TDD is the book [Obey the Testing Goat](https://www.obeythetestinggoat.com/pages/book.html#toc).
- Study docstrings at `__init__.py` files before modifying a package.
- Add section comments at large modules (>300 lines) to group closely-related code blocks.
- Name normal functions and methods as verbs.
- If two linting rules contradict themselves, pick the best one and configure [pyproject.toml](./pyproject.toml) accordingly.
- Frequently make [conventional](https://www.conventionalcommits.org/en/v1.0.0/), atomic commits.
