# .jri/ — Just Ralph It

Project tracking directory managed by [JRI](https://justralph.it).

## Structure

```
tasks/        — Markdown task files with YAML frontmatter, organized by status
  draft/      — tasks being defined
  todo/       — tasks ready to start
  doing/      — tasks in progress
  done/       — completed tasks
uploads/      — user-uploaded reference files
logs/         — application output logs
  app.log     — FastAPI/uvicorn app logs (rotating, 5 MB)
```

Each task is a Markdown file with YAML frontmatter, named by its slug (e.g. `implement-dashboard-page.md`).
Status is determined by which subdirectory the file lives in.
