---
title: Serialize concurrent state writes
priority: 2
assignee: Ralph
depends_on:
  - phase-1-2-quality-gate-foundation
acceptance_criteria:
  - Concurrent state updates do not silently lose fields through read-modify-write races.
  - Documentation explains the chosen concurrency model for `.jri/state.json` writes.
---

`StateStore` now protects against torn writes and keeps a readable backup copy,
but its mutators still do unsynchronized read-modify-write cycles.
If multiple JRI commands touch `.jri/state.json` at the same time,
one update can still clobber another.

Decide whether JRI should use file locking,
compare-and-swap style versioning,
or another serialization strategy,
then make the behavior explicit and test it.
