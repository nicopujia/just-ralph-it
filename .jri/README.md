# .jri/ — JRI Platform Task Tracking

This directory tracks tasks for the JRI platform itself.

## Structure

```
tasks/
  draft/    — tasks still being defined
  todo/     — tasks ready to be worked on
  doing/    — tasks currently in progress
  done/     — completed tasks
```

Each task is a YAML file named by its slug (e.g. `implement-dashboard-page.yaml`).
Status is determined by which subdirectory the file lives in.
