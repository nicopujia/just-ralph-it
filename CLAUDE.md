# Development Instructions

## Workflow

- Use subagents for ANY TASK YOU DO. Use up to 100 subagents for each of them. Parallelize independent ones. If possible, run them in the background so you don't lock the conversation.
- Commit, push, and restart the service (`sudo systemctl restart jri`) frequently.
- Pre-commit hooks enforce formatting (ruff) and type checking (ty).
- Run the full test suite (`uv run pytest tests/`) after large changes, otherwise only relevant integration tests.
- Smoke test after restarting: curl key routes to catch 500s.

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
