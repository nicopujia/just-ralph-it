# JRI (i.e. [Just Ralph It](https://justralph.it), a.k.a. [Ralfealo](https://ralfealo.com))

## Contributing

### Prerrequisites

- [uv](https://docs.astral.sh/uv/)

### Commands

```bash
# Run CLI
uv run jri

# Validation
uv run ruff format
uv run basedpyright
uv run ruff check --fix
uv run pytest -q
```

### Guidelines

- Follow strict TDD
- Make [conventional](https://www.conventionalcommits.org/en/v1.0.0/), atomic commits

## Related resources

- [Concept document](https://nicolaspujia.com/just-ralph-it), by the creator of JRI.
- [Original article about the Ralph technique](https://ghuntley.com/ralph), by [G. Huntley](https://x.com/GeoffreyHuntley), creator of Ralph.
- [The Ralph Playbook](https://claytonfarr.github.io/ralph-playbook/), backed by G. Huntley.
