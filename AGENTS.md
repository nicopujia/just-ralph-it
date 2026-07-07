# Just Ralph It (JRI)

## Overview

This is a pure Python monorepo. To know more about this project and related knowledge, refer to the following documents:

- [Project concept document and vision](https://nicolaspujia.com/just-ralph-it.md) — **TL;DR**: Easily define your own software project idea and then build it entirely by just clicking a button.
- [Ralph technique playbook](https://raw.githubusercontent.com/ClaytonFarr/ralph-playbook/refs/heads/main/README.md)
- [Original article about the Ralph technique](https://ghuntley.com/ralph/)

## Guidelines

### Code style

- Follow Domain-Driven Development principles. For example, if a class about agents is on the `core` package and uses the OpenAI SDK, name it `Agent`, NOT `CoreAgent` or `OpenAIAgent`.
- Name functions and methods as verbs unless they are of a special kind (e.g. decorators, event handlers, etc).
- Write higher-level functions above lower-level ones. For example, if `f()` calls `a()` and then `b()`, write them in that order on the module.
- Prefer defining logic inline instead of splitting into several helper functions unless the logic repeats itself.

### Workflow

- Spin a very capable subagent to manually test via `tmux` your changes as a real user would use JRI in production, sending real messages to real models, and judging the behavior and answers quality.
- Spin a very capable subagent to reduce LOCs of your diff (only logic-wise, not docs-wise) as much as possible while preserving behavior.
- Don't add automated tests unless explicitely asked for.

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
jri --help

# Run formatting, linting, and typechecking; this mutates files.
# Use it always after making changes
./scripts/check.sh

# TUI manual smoke example
smoke_dir="$(mktemp -d)"
tmux new-window -t jri -n smoke "jri"
sleep 4
tmux send-keys -t jri:smoke "I want to build a small app for tracking books I read." Enter
sleep 4
tmux capture-pane -pt jri:smoke
tmux kill-window -t jri:smoke
rm -rf "$smoke_dir"
```
