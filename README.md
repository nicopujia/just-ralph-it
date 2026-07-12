# JRI (_i.e. [Just Ralph It](https://justralph.it), a.k.a. [Ralfealo](https://ralfealo.com)_)

## Overview

Just Ralph It is a tool that aims to help you easily define your software project idea and then build entirely by just clicking a button. To know more about it, read the [concept document](https://nicolaspujia.com/just-ralph-it).

Right now, it only features the Interviewer agent on a terminal user interface (TUI). It will help you define what you actually want to build, and take notes during the process. But you don't even have to think about that! Simply run `jri` and start sharing your thoughts, and the Interviewer will handle the rest. While you have your conversation, JRI manages the `.jri/` directory inside your project.

So for now, you can hand the `.jri/graph.json` file to your favorite coding agent to help you implement it. Tip: ask it to write a plan first.

## Getting started

### Prerrequisites

- Python >=3.13 or [uv](https://docs.astral.sh/uv/)

### Installation

```bash
pip install just-ralph-it
# or
uv tool install just-ralph-it
```

### Authentication

Given that JRI is powered by LLMs, you need to provide an inference provider. You can authenticate with an API key from any OpenAI-compatible provider or reuse an existing ChatGPT subscription.

For both cases, the easiest way to authenticate is by creating a `.env` file at the root of your project or home directory. You can also `export` the variables in your shell or pass them as CLI arguments (see `jri --help`).

#### API key approach

```bash
# To use OpenAI as the provider, just set your OpenAI key here
JRI_LLM_API_KEY=...

# To use any OpenAI-compatible provider, provide its base URL too
JRI_LLM_PROVIDER=https://provider.example/v1
JRI_LLM_API_KEY=...
```

#### ChatGPT approach

To use a ChatGPT subscription, you need to have Codex installed and configure it to store credentials in a file:

```toml
# ~/.codex/config.toml
cli_auth_credentials_store = "file"
```

Then run:

```bash
codex login
```

Finally, configure the JRI provider:

```bash
# Configure JRI to reuse the Codex login
JRI_LLM_PROVIDER=openai-codex
```

### Usage

```bash
# Create a new directory for your project or just move to an existing one
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

### Setup recommendation

JRI is still under heavy development, so not all defaults have been set yet. However, as the developer, I have experienced pretty good and cheap results with the following configuration:

```bash
# .env
JRI_LLM_PROVIDER=https://openrouter.ai/api/v1
JRI_LLM_API_KEY=sk-or-v1-...
JRI_INTERVIEWER_MODEL=z-ai/glm-5.2
JRI_EXPLORER_MODEL=qwen/qwen3.5-9b
JRI_BRAVE_SEARCH_API_KEY=...
```

## Uninstalling

```bash
# Uninstall JRI and its dependencies
pip uninstall just-ralph-it
# or
uv tool uninstall just-ralph-it

# Remove JRI from a repo
rm -fr /path/to/your-project/.jri
```
