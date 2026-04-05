---
title: Track make check pass/fail metrics
priority: 2
assignee: Ralph
depends_on: []
acceptance_criteria:
  - A file `.jri/metrics.json` is created/updated after each `make check` run, recording pass/fail per iteration.
  - Each entry includes: iteration number, task slug, timestamp, and pass/fail result.
  - The file is valid JSON (an array of entries) and survives across iterations (appended, not overwritten).
  - `.jri/metrics.json` is listed in `.jri/.gitignore` (ephemeral runtime data, not tracked in git).
  - `jri status` shows a summary: total runs, pass count, fail count, and pass rate percentage.
  - Existing `make check` behavior is unchanged — metrics are recorded observationally without altering the pass/fail logic.
  - make check passes.
---

Track `make check` outcomes across iterations so the human operator can assess whether tests are actually catching real issues or just noise.

### Storage

Dedicated file `.jri/metrics.json` — a JSON array of objects. Each object:

```json
{
  "iteration": 3,
  "task": "some-slug",
  "ts": "2026-04-05T14:30:00Z",
  "result": "pass"
}
```

The file is appended to on each `make check` run. It is not tracked in git (add to `.jri/.gitignore`).

### Recording

In `service.py`, the `make check` path (around line 725-780) already has a clear success/failure branch. Record a metric entry in both branches — before the recovery/continuation logic.

Also record a `pass` entry on the happy path when `make check` succeeds (currently no explicit event for that — only a `make_check_passed` timeline event exists). The metric entry is separate from timeline events.

### Display

Add a summary line to `jri status` output, e.g.:

```
metrics: 12 runs, 10 pass, 2 fail (83% pass rate)
```

Show nothing if `.jri/metrics.json` does not exist (no metrics yet).

### Implementation notes

- Create a small `MetricsStore` class (similar to `TimelineStore`) in a new file `src/jri/core/metrics.py`.
- Add a `metrics_path` property to `JriPaths`.
- Add `.jri/metrics.json` to the `.jri/.gitignore` content written by `_write_managed_files()`.
- The store loads the existing array, appends, and saves atomically.
- The `jri status` command reads and summarizes when the file exists.
