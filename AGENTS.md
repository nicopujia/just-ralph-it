# Just Ralph It (JRI) 

## Context

To know more about this project and related knowledge, refer to the following documents:

- [Project concept document and vision](https://nicolaspujia.com/just-ralph-it.md)
- [Ralph technique playbook](https://raw.githubusercontent.com/ClaytonFarr/ralph-playbook/refs/heads/main/README.md)
- [Original article about the Ralph technique](https://ghuntley.com/ralph/)

## Commands

```bash
# Manage dependencies
uv sync --all-groups
uv add [package]
uv add --dev [package]
uv remove [package]

# Run TUI
jri

# Run after making changes
./scripts/check.py
```

## Guidelines

### Workflow

- Study docstrings at `__init__.py` files before modifying a package, as they contain relevant contributing information specific to that package.
- When asked to commit, frequently make [**conventional**](https://raw.githubusercontent.com/conventional-commits/conventionalcommits.org/refs/heads/master/content/v1.0.0/index.md) and **atomic** commits.
- Manually test your changes as a real user would use the software in production (e.g. via `tmux`). Don't add automated tests unless explicitely asked for.

### Code style

- Add section comments at large modules (>300 lines) to group closely-related code blocks.
- Name normal functions and methods as verbs.
- If two linting rules contradict themselves, pick the best one and configure [pyproject.toml](./pyproject.toml) accordingly.
