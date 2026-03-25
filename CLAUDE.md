# Development Instructions

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

## Workflow

- Use subagents for each task. Parallelize independent tasks.
- Commit, push, and restart the service (`sudo systemctl restart jri`) frequently.

