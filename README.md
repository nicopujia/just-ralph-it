# JRI (i.e. [Just Ralph It](https://justralph.it), a.k.a. [Ralfealo](https://ralfealo.com))

## Getting started

### Prerrequisites

- [uv](https://docs.astral.sh/uv/)

### Setup

```bash
# Clone the repo
git clone https://github.com/nicopujia/just-ralph-it
cd just-ralph-it

# Install dependencies
uv sync

# Install JRI globally
uv tool install -e .
```

### Usage

```bash
# Create a new directory for your project
# or just move to an existing one
mkdir /path/to/your-project
cd /path/to/your-project

# Run the TUI
# This will ask for the necessary environment variables
# Once that's ready, it will also set the .jri/ directory up
jri

# See all available options
jri --help

# Visualize the notes graph
jri view
```

## Uninstalling

```bash
# Uninstall JRI and its dependencies
uv tool uninstall just-ralph-it

# Remove JRI from a repo
cd /path/to/your-project/.jri
rm -fr .jri/
```

## Contributing

Please refer to [AGENTS.md](./AGENTS.md).
