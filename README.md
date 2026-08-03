# [Just Ralph It](https://justralph.it) (a.k.a. _[Ralfealo](https://ralfealo.com)_)

## Overview

Just Ralph It (JRI) is a software that helps you think through your software project idea, and then to build it with one click. You just have to share your thoughts, and JRI handles the rest.

> [!NOTE]
> To know more about this project vision and motivation, read the [concept document](https://nicolaspujia.com/just-ralph-it).

### Feature Roadmap

- [x] Terminal UI
- [x] Requirements gathering interview
  - [x] Exploration
    - [x] Local shell commands
    - [x] Local files, including images
    - [x] Web search
    - [x] Public URLs, including YouTube videos
  - [x] Note taking
  - [x] Topic-based context management
- [ ] Automated implementation (inspired on the [SDLC](https://en.wikipedia.org/wiki/Systems_development_life_cycle))
  - [x] Requirements analysis
  - [x] System design
  - [ ] Development
  - [ ] Quality assurance
  - [ ] Deployment
  - [ ] Maintenance
- [ ] Remote execution
- [ ] Web UI
  - [x] Notes graph visualization
- [ ] Hosted platform

## Getting Started

### 0. Prerequisites

- [Python](https://www.python.org/downloads/) >=3.13 or [uv](https://docs.astral.sh/uv/getting-started/installation/)
- API key from any [OpenAI-compatible](https://github.com/openai/openai-python) LLM inference provider OR a [ChatGPT](https://chatgpt.com/codex/pricing/) subscription

### 1. Installation

```bash
pip install just-ralph-it
# or
uv tool install just-ralph-it
```

### 2. Setup

Move to a project directory and set it up:

```bash
mkdir ./my-project
cd ./my-project
jri init
```

That creates `.jri/config.yaml`. Open it and follow its comments.

### 3. Usage

```bash
# Run the TUI and start chatting!
jri chat

# Visualize the notes graph
jri view
```

> [!TIP]
> Given that there isn't automated implementation yet, you can hand the generated `.jri/notebook.yaml` or `.jri/specs/` to your favorite coding agent as a starting point for implementation.

## Upgrading

```bash
pip install --upgrade just-ralph-it
# or
uv tool upgrade just-ralph-it
```

> [!WARNING]
> Until reaching a stable version (>=1.0.0), anything could break from one version to another.

## Uninstalling

```bash
pip uninstall just-ralph-it
# or
uv tool uninstall just-ralph-it

# Remove it from your project
rm -fr /path/to/your-project/.jri
```
