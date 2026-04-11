# Just Ralph It (JRI)

> The proper tool around the Ralph Wiggum technique.

## Quickstart

### Prerequisites

- **Mandatory**: [Git](https://git-scm.com/install/), [Make](https://www.gnu.org/software/make/), [uv](https://docs.astral.sh/uv/getting-started/installation/), [OpenCode](https://opencode.ai/docs/#install)
- **Recommended**: VPS or [Docker](https://docs.docker.com/get-started/get-docker/)

### Install and run

```bash
uv tool install -e .
jri --help
```

## Docs

1. [Architecture](./docs/arch.md)
2. [Contributing](./docs/contrib.md)
3. [Roadmap](./docs/roadmap.md)

## CLI

JRI uses top-level commands:

```bash
jri chat [--fresh] [-m MODEL] [--validator-model MODEL] [opencode args...]
jri status
jri timeline [--task SLUG] [--json]
jri inspect [SLUG]
jri init [DIRECTORY] [--force | --upgrade]
jri start [-n TASKS] [-d] [-m MODEL] [--validator-model MODEL] [--task-timeout SECONDS] [--force]
jri attach
jri stop [REASON]
jri halt
jri reset [TASK] [--force]
```

- `jri chat --model` overrides the `interrogator` model for one chat run.
- `jri chat --validator-model` overrides the `interrogator-validator` model for one chat run.
- `jri start --model` overrides the `ralph` model for one loop run.
- `jri start --validator-model` overrides the `ralph-validator` model for one loop run.
- `jri inspect` prints the saved log for a task slug, or the active/latest attempt when no slug is passed.
- `jri attach` follows the currently tracked Ralph run after a detached start.

---

## Related resources

- [Original article about the Ralph technique](https://ghuntley.com/ralph), by [G. Huntley](https://x.com/GeoffreyHuntley), creator of Ralph.
- [The Ralph Playbook](https://claytonfarr.github.io/ralph-playbook/), backed by G. Huntley.
- [JRI concept](https://nicolaspujia.com/ralph), by [Nicolás Pujia](https://x.com/nicopujia), creator of JRI.
