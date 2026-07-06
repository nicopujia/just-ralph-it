# Just Ralph It (JRI)

## Overview

This is a pure Python monorepo. To know more about this project and related knowledge, refer to the following documents:

- [Project concept document and vision](https://nicolaspujia.com/just-ralph-it.md) — **TL;DR**: Easily define your own software project idea and then build it entirely by just clicking a button.
- [Ralph technique playbook](https://raw.githubusercontent.com/ClaytonFarr/ralph-playbook/refs/heads/main/README.md)
- [Original article about the Ralph technique](https://ghuntley.com/ralph/)

## Commands

```bash
# Manage dependencies
uv sync --all-groups
uv add [package]
uv add --dev [package]
uv remove [package]

# Install CLI globally
uv tool install -e .

# Run CLI anywhere
jri

# Run after making changes
./scripts/check.sh
```

## Guidelines

### Code style

- Follow Domain-Driven Development principles. For example, if a class about agents is on the `core` package and uses the OpenAI SDK, name it `Agent`, NOT `CoreAgent` or `OpenAIAgent`.
- Name functions and methods as verbs unless they are of a special kind (e.g. decorators, event handlers, etc).
- Write higher-level functions above lower-level ones. For example, if `f()` calls `a()` and then `b()`, write them in that order on the module.
- Prefer defining logic inline instead of splitting into several helper functions unless the logic repeats itself.

### Workflow

- Spin a subagent to manually test (e.g. via `tmux`) your changes as a real user would use JRI in production.
- Spin a subagent to reduce LOCs of your diff (only logic-wise, not docs-wise) as much as possible while preserving behavior.
- Don't add automated tests unless explicitely asked for.
