# Development Instructions

## Workflow

- Use subagents for ANY TASK YOU DO. Use up to 100 subagents for each of them. Parallelize independent ones.
- Commit, push, and restart the service (`sudo systemctl restart jri`) frequently.

## Documentation

- Update existing docs whenever code changes.
- Document the non-obvious concisely.
- Do not over-use em-dashes.

## Commit Messages

Use conventional commits.
Mostly lowercase.
Abbreviate when obvious (e.g. `deps`, `cfg`, `init`, `impl`, `refactor`, `rm`, `mv`, etc.).
Keep subjects short.
If you include a body, keep it concise.

### Examples

```
feat: impl username/password auth

Closes: ex-001
```
```
docs: update setup instructions in README

Replace pip with uv.
```

## Formatting and Linting

- Run `uv run ruff format .` before committing.
- Run `uv run ruff check --fix .` before committing.

## Type Checking

- Run `ty check` before committing.
