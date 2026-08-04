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
- Helper functions only for repeated logic, unavoidable extractions, or addressing linter alerts
- Module members in groups one blank line apart: dunders (eg `__all__`), types (eg `type ChatEvent = ...`), constants (never private), variables (eg `logger`), public functions, public classes, private functions, private classes
- Class members: constants, magic methods, nested types (eg `MessageInput.Submitted`), public methods, private methods; decorated methods sort by visibility (a public `@property` is a public method)
- Import paths <=3 levels

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
