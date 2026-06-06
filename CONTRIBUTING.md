# Contributing

```bash
# Install all dependencies, including development ones
uv sync --all-groups

# Install Git hooks
uv run pre-commit install --hook-type pre-commit --hook-type pre-push

# Run CLI inside this repo
uv run jri --help

# Validate changes
./scripts/validate.py --help
```

## Guidelines

### Workflow

Pre-commit runs formatting, linting, and type checking. Pre-push runs full validation, including test suite. With that in mind, stick to the following workflow:

1. work on a branch;
2. frequently make [conventional](https://www.conventionalcommits.org/en/v1.0.0/), atomic commits;
3. manually test your changes as a real user would use the product;
3. once you think the work is done, push and request a review;
4. after a successful review with another contributor, squash and merge;
5. delete the new remote branch afterwards.

IMPORTANT:
- Follow strict test-driven development (TDD), maintaining 100% coverage of [source code](./src/) at all times. A good resource on TDD is the book [Obey the Testing Goat](https://www.obeythetestinggoat.com/pages/book.html#toc).
- Study docstrings at `__init__.py` files before modifying a package.

### Style

- Add section comments at large modules (>300 lines) to group closely-related code blocks.
- Name normal functions and methods as verbs.
- If two linting rules contradict themselves, pick the best one and configure [pyproject.toml](./pyproject.toml) accordingly.
