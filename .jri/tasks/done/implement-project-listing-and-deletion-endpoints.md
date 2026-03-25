---
title: Implement project listing and deletion endpoints
priority: 0
assignee: ralph
depends_on:
- implement-project-creation-with-github-repo-initialization
created: '2026-03-21'
acceptance_criteria:
- GET /api/projects returns an array of the user's projects with issue_count populated.
- GET /api/projects/{name} returns the full project details for an existing project.
- GET /api/projects/nonexistent returns
- 4. DELETE /api/projects/{name}?delete_repo=true deletes the local directory, the
  SQLite row, and the GitHub repo.
- DELETE /api/projects/{name}?delete_repo=false deletes the local directory and SQLite
  row but leaves the GitHub repo.
- DELETE on a project with ralph_loop_status='running' returns
- 7. After deletion, GET /api/projects no longer includes the deleted project.
---

Add list and delete endpoints in app/routers/projects.py.

## GET /api/projects
- Requires auth.
- Query all projects for the current user from SQLite.
- For each project, count the number of beads issues by running `bd list --json` in the project directory and counting the returned array length. If the project directory doesn't exist or bd fails, return 0.
- Return JSON array: [{ "id": ..., "name": ..., "description": ..., "github_repo_url": ..., "issue_count": ..., "ralph_loop_status": ..., "created_at": ... }, ...]

## DELETE /api/projects/{name}
- Requires auth.
- Query param: `delete_repo` (boolean, default true).
- Verify the project belongs to the current user. Return 404 if not found.
- If ralph_loop_status is 'running', return 409 with message 'Cannot delete project while Ralph is running'.
- If delete_repo is true: DELETE https://api.github.com/repos/ralphpujia/{name} using the ralphpujia token. Ignore 404 (repo may already be deleted).
- Delete the project directory recursively: shutil.rmtree(project_path).
- Delete the project row from SQLite (CASCADE will delete notifications too).
- Return 204 No Content.

## GET /api/projects/{name}
- Requires auth.
- Verify the project belongs to the current user. Return 404 if not found.
- Return the project JSON (same shape as list items) plus: ralph_session_id, ralph_loop_current_issue, ralph_loop_iteration.
