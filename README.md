# Just Ralph It

Describe your project. Ralph builds it.

## Architecture

```
User <-> nginx (port 80) <-> uvicorn/FastAPI (port 8000)
                                  |
                    +-------------+-------------+
                    |             |              |
                 SQLite       Ralphy          Ralph
                (jri.db)   (interviewer)    (builder)
                             Claude CLI     Claude CLI
```

### AI Agents

- **Ralphy**: interviews users to understand their project, creates detailed issues (Claude Opus via CLI)
- **Ralph**: picks up open issues one by one, implements via TDD (Claude Opus via CLI)

### Tech Stack

- **FastAPI** with Jinja2 templates, SSE for real-time streaming
- **SQLite** for app metadata (users, projects, sessions)
- **GitHub**: OAuth login + `ralphpujia` bot account creates repos per project
- **Stripe**: per-project payments
- **nginx + Cloudflare**: reverse proxy, SSL, subdomain routing for deployed projects

## Local setup

```bash
# 1. Clone
git clone https://github.com/ralphpujia/jri.git && cd jri

# 2. Install Python dependencies
uv sync

# 3. External tools (must be on PATH)
#    - claude (Anthropic CLI)
#    - gh (GitHub CLI, authenticated as the bot account)

# 4. Configure environment
cp example.env .env
# Edit .env with your credentials

# 5. Run
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
# App starts at http://127.0.0.1:8000
```

## Ralph Task System

Internal tasks live in `.ralph/tasks/` as YAML files, organized by lifecycle stage:

```
.ralph/tasks/
  draft/     # ideas, not yet ready
  todo/      # ready to be picked up
  doing/     # currently in progress
  done/      # completed
```

Each YAML file has a `title`, `priority`, `assignee`, `depends_on`, `created`, and `acceptance_criteria`. Tasks are moved between directories as they progress.

## Session & Auth

- **GitHub OAuth**: redirects to GitHub, exchanges code for token, fetches user profile, creates/updates local user record
- **Session tokens**: signed with `itsdangerous`, stored as `session` cookie, 30-day expiry (`SESSION_MAX_AGE`)

## SSE Event Types

Real-time updates via `app/sse_bus.py`, keyed by project name:

`issue_update`, `claude_md_update`, `ralph_stdout`, `ralph_status`, `notification`, `ralphy_processing`

## Upload Constraints

- **Max multipart size**: 10 MB (set in `app/main.py` via `MultiPartParser.max_file_size`)
- **Max attachment size**: 3 MB per file (enforced in `app/routers/chat.py`)
- **nginx limit**: `client_max_body_size 10M`
- Path traversal (`..`, `/`) is rejected

## Maintenance Mode

Set `MAINTENANCE_MODE=1` (or `true`/`yes`) to block new project creation. When active, the create-project endpoint returns 503 and appends the user's email to `./data/waitlist.txt`.

## Testing

```bash
uv run pytest                                    # unit tests
uv run pytest tests/e2e_test.py -v --timeout=120 # e2e tests
uv run ruff check .                              # lint
```

## Data

| What | Where |
|------|-------|
| App database (users, projects) | `./data/jri.db` (SQLite) |
| Project repos + uploads | `./data/<github-username>/<project-name>/` |
| Ralphy session files | `~/.claude/projects/-home-nico-jri-data-<user>-<project>/` |
| Auth credentials | `./.env`, `~/.config/gh/hosts.yml`, `~/.claude/.credentials.json` |
| Waitlist (maintenance mode) | `./data/waitlist.txt` |

## Logs

| What | Where |
|------|-------|
| App logs (requests + errors) | `journalctl -u jri -f` |
| Ralphy conversations | `~/.claude/projects/-home-nico-jri-data-<user>-<project>/<session-id>.jsonl` |
| Ralph loop conversations | Same path, session ID from `ralph_loop.py` logs |
| nginx access/error | `/var/log/nginx/access.log`, `/var/log/nginx/error.log` |
