# JRI (i.e. [Just Ralph It](https://justralph.it), a.k.a. [Ralfealo](https://ralfealo.com))

## Getting started

### Prerequisites

- [uv](https://docs.astral.sh/uv/)
- Python 3.13 or newer

### Installation

```bash
uv tool install just-ralph-it
```

### Authentication

You can authenticate with an API key or reuse an existing ChatGPT Codex subscription.

To use an API key:

```bash
# Use OpenAI
export JRI_LLM_API_KEY=...

# Or use any OpenAI-compatible provider
export JRI_LLM_PROVIDER=https://provider.example/v1
export JRI_LLM_API_KEY=...
```

You can also use a `.env` file or CLI arguments.

To use a ChatGPT subscription, configure Codex to store credentials in a file:

```toml
# ~/.codex/config.toml
cli_auth_credentials_store = "file"
```

Then run:

```bash
# Log in with ChatGPT
codex login

# Configure JRI to reuse the Codex login
export JRI_LLM_PROVIDER=openai-codex
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
