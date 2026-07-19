# Just Ralph It (JRI)

## Overview

This is a pure Python project. To know more about it and related knowledge, refer to the following documents:

- [Project concept document and vision](https://nicolaspujia.com/just-ralph-it.md) — **TL;DR**: Easily define your own software project idea and then build it entirely by just clicking a button.
- [Ralph technique playbook](https://raw.githubusercontent.com/ClaytonFarr/ralph-playbook/refs/heads/main/README.md)
- [Original article about the Ralph technique](https://ghuntley.com/ralph/)

## Guidelines

### Code style

- Follow DDD principles, especially for naming conventions. For example:
    - If a class about agents is on the `core` package and uses the OpenAI SDK, name it `Agent`, NOT `CoreAgent` or `OpenAIAgent`.
    - If a class about Notes has methods for managing notes, don't include the word "note" in those methods.
- Name functions and methods as verbs unless they are of a special kind (e.g. decorators, event handlers, etc).
- Write docstrings, but only for public members.
- Write higher-level functions above lower-level ones. For example:
    - If `f()` calls `a()` and then `b()`, write them in that order on the module.
- Define logic inline instead of splitting into multiple helper functions unless the logic repeats itself.
- Internal import paths must not exceed three levels, including `jri` (for example, `jri.core.agents`). Expose deeper
  members from a package entry point instead of importing their implementation module directly.

### Automated testing

- Follow the 80/20 rule.
- Keep tests deterministic. Do not access live models or networks.
- Functional tests must be black-box style — they shouldn't know how JRI is implemented. Use real temporary files and replace only external boundaries such as LLMs, HTTP services, and clocks.

### Avoid (unless explicitely asked for the opposite)

- Do NOT write, and wipe out immediately if existing, all code related to handling states of previous versions. Anything legacy or related to backwards compatibility must be outright deleted.
- Do NOT write defensive code for hypothetical situations. Do NOT handle edge cases unless asked for. Keep the happy path direct and trust internal types, invariants, and required data.
- Do NOT write tests for private methods, internal call sequences, configurations, documentation, prompts, model responses, or dependency behavior.

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

# Run project's automated checks (linter, tests, etc.)
# Use it always after making changes
./scripts/check.py
```
