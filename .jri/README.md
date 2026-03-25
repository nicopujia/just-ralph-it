# .jri/ — Just Ralph It

Project tracking directory managed by [JRI](https://justralph.it).

## Structure

```
tasks/        — YAML task files organized by status
  draft/      — tasks being defined
  todo/       — tasks ready to start
  doing/      — tasks in progress
  done/       — completed tasks
uploads/      — user-uploaded reference files
logs/         — application and agent output logs
  app.log     — FastAPI/uvicorn app logs (rotating, 5 MB)
  ralph.log   — Ralph (builder) output log
  ralphy.log  — Ralphy (interviewer) output log
```

Each task is a YAML file named by its slug (e.g. `implement-dashboard-page.yaml`).
Status is determined by which subdirectory the file lives in.
