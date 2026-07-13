# [Just Ralph It](https://justralph.it) (_a.k.a. [Ralfealo](https://ralfealo.com)_)

## Overview

Just Ralph It is a software system that aims to help you easily define your software project idea and then build entirely by just clicking a button. To know more about it, read the [concept document](https://nicolaspujia.com/just-ralph-it).

### Feature Roadmap

- [x] Terminal UI
- [x] Interviewer
  - [x] Exploration
  - [x] Note taking
  - [x] Topic-based context management
- [ ] Project implementation
- [ ] Remote execution
- [ ] Web UI
  - [x] Notes graph visualization
- [ ] Hosted platform

## Getting Started

### 0. Prerrequisites

- Python >=3.13 or [uv](https://docs.astral.sh/uv/)
- API key from any OpenAI-compatible LLM inference provider or a ChatGPT subscription

### 1. Installation

```bash
pip install just-ralph-it
# or
uv tool install just-ralph-it
```

### 2. Authentication

You can create a `.env` file at the root of your project or home directory. You can also `export` the variables in your shell or pass them as CLI arguments (see `jri --help`).

#### Using an API key

```bash
# .env

# To use OpenAI as the provider, just set your OpenAI key here
JRI_LLM_API_KEY=...

# To use any OpenAI-compatible provider, provide its base URL too
JRI_LLM_PROVIDER=https://provider.example/v1
JRI_LLM_API_KEY=...
```

#### Using a ChatGPT subscription

For this, you need to have Codex installed and configure it to store credentials in a file:

```toml
# ~/.codex/config.toml
cli_auth_credentials_store = "file"
```

Then run:

```bash
codex login
```

Finally, configure it as the provider:

```bash
# .env

JRI_LLM_PROVIDER=openai-codex
```

#### Optional: Brave Search

To support web search, provide a [Brave Search API](https://brave.com/search/api/) key:

```bash
# .env

JRI_BRAVE_SEARCH_API_KEY=...
```

### 3. Usage

```bash
# Run the TUI and start chatting!
jri

# Visualize the notes graph
jri view
```

> [!TIP]
> Given that JRI doesn't feature project implementation yet, you can hand the generated `.jri/graph.json` file to your favorite coding agent as a starting point for implementation.

## Upgrading

```bash
python -m pip install --upgrade just-ralph-it
# or
uv tool upgrade just-ralph-it
```

> [!WARNING]
> Until JRI reaches a stable version (>=1.0.0), anything could break from one version to another.

## Uninstalling

```bash
# Uninstall JRI and its dependencies
pip uninstall just-ralph-it
# or
uv tool uninstall just-ralph-it

# Remove JRI from a repo
rm -fr /path/to/your-project/.jri
```

## Contributing

Please refer to [AGENTS.md](./AGENTS.md).
