# Development

## Build

Build distribution artifacts with:

```bash
uv build
```

This produces a wheel and source distribution under `dist/`.

## Development Loop

Set up the local environment with:

```bash
uv sync
```

During development, run the CLI from the repo with:

```bash
uv run jri ...
```

or:

```bash
uv run python -m jri ...
```

The project is installed in editable mode, so Python source changes under
`src/jri/` are picked up immediately. If you change dependencies or packaging
metadata, run `uv sync` again.

## Validation

Typical checks:

```bash
uv run ruff check .
uv run ty check
uv run pytest
```

The opt-in live OpenCode test can be run with:

```bash
uv run pytest tests/live/test_live_opencode.py --run-live-opencode --opencode-model opencode/qwen3.6-plus-free
```
