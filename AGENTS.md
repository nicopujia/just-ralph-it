# Just Ralph It (JRI)

> Think through a software project idea, then build it with one click.

For more info, refer to the [concept document](https://nicolaspujia.com/just-ralph-it.md).

## Conventions

- Trust types and JRI-managed data
- Clean out any code related to backwards compatibility
- `lib` is merely for JRI-agnostic business logic
- `tui` is merely for UI-related code

## Code style

- DDD naming (eg just `Agent.get_context`, NEVER `BaseOpenAIAgent.get_agent_context`)
- Function and method names as verbs (except for event handlers and decorators)
- Higher-level or public functions above lower-level or private ones
- Helper functions only for repeated logic, unavoidable extractions, or addressing linter alerts
- Import paths <=3 levels

## Automated testing

- Only for business logic (`core`, `lib`)
- 80/20-based
- Black-box style
- Deterministic
- Local-only

## Commands

```bash
# Manage deps
uv sync --all-groups
uv add [dep]
uv add --dev [dep]
uv remove [dep]

# Install CLI globally
uv tool install -e .

# Run CLI anywhere
jri --help

# Run automated checks (linter, tests, etc)
# Always use after making changes
./scripts/check.py
```
