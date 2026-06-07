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

# Add dependencies
uv add [package name]

# Add development-only dependencies
uv add --dev [package name]

# Remove dependencies
uv remove [package name]
```

## Guidelines

### Workflow

- Alongside existing codebase and documentation, study docstrings at `__init__.py` files before modifying a package because they contain relevant contributing information specific to that package.
- Frequently make [conventional](https://www.conventionalcommits.org/en/v1.0.0/), atomic commits;
- Manually test your changes as a real user would use the software in production.

### Style

- Add section comments at large modules (>300 lines) to group closely-related code blocks.
- Name normal functions and methods as verbs.
- If two linting rules contradict themselves, pick the best one and configure [pyproject.toml](./pyproject.toml) accordingly.
