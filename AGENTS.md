# Just Ralph It (JRI)

> Think through a software project idea, then build it with one click.

For more info, refer to the [concept document](https://nicolaspujia.com/just-ralph-it.md).

## Conventions

- Trust types and JRI-managed data
- Clean out any code related to backwards compatibility
- `lib` is merely for JRI-agnostic business logic
- `tui` is merely for UI-related code

## Code style

- Apply DDD naming, modules included (eg: `Agent.get_context`, NEVER `BaseOpenAIAgent.get_agent_context`; `repository.py`, NEVER `constants.py`).
- Name functions and methods as verbs, except for event handlers and decorators.
- Write helper functions only for repeated logic, unavoidable extractions, or addressing linter alerts.
- Keep import paths at 3 levels or less.

### Ordering

- **Module members (groups one blank line apart)**: dunders, types, constants (all must be public), public variables, private variables, public functions, public classes, private functions, private classes.
- **Class members**: constants, nested types, magic methods, public methods, private methods.

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
