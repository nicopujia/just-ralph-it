# Just Ralph It (JRI)

> Turn human intent into validated tasks that autonomous agents execute until the intended software converges.

The Interrogator pressure-tests intent until guesses become explicit, user-confirmed assumptions.
Ralph is the execution engine: it works only inside that validated intent, then reports the concrete runtime outcome back to JRI.

## Quickstart

### Prerequisites

- **Mandatory**: [Git](https://git-scm.com/install/), [Make](https://www.gnu.org/software/make/), [uv](https://docs.astral.sh/uv/getting-started/installation/), [Bun](https://bun.sh/), [Pi](https://pi.dev/docs/latest)
- **Recommended**: Run on a VPS. For example, [Contabo](https://contabo.com/en/vps)'s are very cheap.

### Install and run

```bash
uv tool install -e .
jri --help
```

## Docs

1. [Architecture](./docs/arch.md)
2. [Contributing](./docs/contrib.md)
3. [Roadmap](./docs/roadmap.md)

---

## Related resources

- [Original article about the Ralph technique](https://ghuntley.com/ralph), by [G. Huntley](https://x.com/GeoffreyHuntley), creator of Ralph.
- [The Ralph Playbook](https://claytonfarr.github.io/ralph-playbook/), backed by G. Huntley.
- [JRI concept](https://nicolaspujia.com/ralph), by [Nicolás Pujia](https://x.com/nicopujia), creator of JRI.
