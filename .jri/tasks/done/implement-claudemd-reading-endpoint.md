---
title: Implement CLAUDE.md reading endpoint
priority: 1
assignee: ralph
depends_on:
- implement-project-creation-with-github-repo-initialization
created: '2026-03-21'
acceptance_criteria:
- GET /api/projects/{name}/claude-md for a project with an CLAUDE.md returns the raw
  markdown content.
- The response includes an 'exists' boolean field.
- For a project without CLAUDE.md, returns empty content and exists=false.
- For an invalid project, returns 404.
---

Create an endpoint to read the project's root CLAUDE.md.

## GET /api/projects/{name}/claude-md
- Requires auth. Verify project belongs to user.
- Read the file at {project_dir}/CLAUDE.md.
- If the file doesn't exist, return {"content": "", "exists": false}.
- If it exists, return {"content": "<raw markdown string>", "exists": true}.
- The frontend will render this as HTML using a markdown library.
