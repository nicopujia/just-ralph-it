# JRI (i.e. [Just Ralph It](https://justralph.it), a.k.a. [Ralfealo](https://ralfealo.com))

## Contributing

### Prerrequisites

- [uv](https://docs.astral.sh/uv/)

### Commands

```bash
# Setup
git clone https://github.com/nicopujia/just-ralph-it
cd ./just-ralph-it
uv sync --all-groups

# Run CLI inside this repo
uv run jri --help

# Install CLI globally and run it anywhere
uv tool install -e .
jri --help

# Validate changes
# Always run it after making changes
./scripts/validate.py
```

### Guidelines

- Follow strict TDD maintaining 100% coverage of [source code](./src/).
- Integration tests must follow a black box approach (i.e. assume the implementation is unknown and focus only on the behavior).
- Make [conventional](https://www.conventionalcommits.org/en/v1.0.0/), atomic commits.

## Related resources

- [Concept document](https://nicolaspujia.com/just-ralph-it), by the creator of JRI.
- [Original article about the Ralph technique](https://ghuntley.com/ralph), by [G. Huntley](https://x.com/GeoffreyHuntley), creator of Ralph.
- [The Ralph Playbook](https://claytonfarr.github.io/ralph-playbook/), backed by G. Huntley.
