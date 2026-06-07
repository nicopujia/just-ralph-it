# Contributing

```bash
# Install all dependencies, including development ones
uv sync --all-groups

# Run CLI inside this repo
uv run jri --help

# Validate changes
# This command runs linter, formatter, and typechecker
# Run it frequently while you make changes
# And also test your changes manually as a user would in production!!!
./scripts/validate.py
```

## Guidelines

### Workflow

It's crucial that when you make changes, you stick to the following workflow:

0. Besides existing codebase and documentation, study docstrings at `__init__.py` files before modifying a package because they contain relevant contributing information specific to that package.
1. work on a branch;
2. frequently make [conventional](https://www.conventionalcommits.org/en/v1.0.0/), atomic commits;
3. manually test your changes as a real user would use the product;
3. once you think the work is done, create a pull request and request a review;
4. after a successful review with another contributor and running validation in full mode, squash and merge;
5. delete the new remote branch afterwards.

### Style

- Add section comments at large modules (>300 lines) to group closely-related code blocks.
- Name normal functions and methods as verbs.
- If two linting rules contradict themselves, pick the best one and configure [pyproject.toml](./pyproject.toml) accordingly.
