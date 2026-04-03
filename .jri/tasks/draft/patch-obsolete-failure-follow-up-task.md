---
title: Resolve obsolete failure follow-up task
priority: 0
assignee: Human
depends_on: []
acceptance_criteria:
  - The promoted task `turn-failures-into-follow-up-work` is explicitly reviewed against the updated roadmap.
  - A decision is recorded to either keep it as docs/prompt alignment work or supersede it as obsolete.
  - Ralph is not left to guess whether that promoted task is still expected to run.
---

The roadmap no longer says generic failures must create new tasks.

That makes `turn-failures-into-follow-up-work` look obsolete or at least mislabeled. Since promoted tasks should not be edited in place, this patch task records the need to explicitly resolve that mismatch before execution.
