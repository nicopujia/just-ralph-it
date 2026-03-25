# Just Ralph It

The proper tool around the Ralph Wiggum technique. Read the [PRD](https://nicolaspujia.com/ralph).

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

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [claude](https://docs.anthropic.com/en/docs/claude-cli) CLI (Anthropic CLI, must be on PATH)
- [gh](https://cli.github.com/) CLI (authenticated as the bot account, must be on PATH)

### Steps

```bash
# 1. Clone
git clone https://github.com/nicopujia/justralph.it.git && cd justralph.it

# 2. Install Python dependencies
uv sync

# 3. Configure environment
cp example.env .env
# Edit .env -- at minimum set SECRET_KEY, GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET

# 4. Run
uv run uvicorn app.main:app --app-dir src --reload --host 127.0.0.1 --port 8000
# App starts at http://127.0.0.1:8000
```

The SQLite database (`./data/jri.db`) is created automatically on first run.

## Ralph Task System

Internal tasks live in `.jri/tasks/` as YAML files, organized by lifecycle stage:

```
.jri/tasks/
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

Real-time updates via `src/app/sse_bus.py`, keyed by project name:

`issue_update`, `claude_md_update`, `ralph_stdout` (live-streamed, not persisted to disk), `ralph_status`, `notification`, `ralphy_processing`

## Upload Constraints

- **Max multipart size**: 10 MB (set in `src/app/main.py` via `MultiPartParser.max_file_size`)
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

## Deployment

Each project can be deployed to `{name}.justralph.it`:

- A **systemd unit** (`jri-deploy-{name}.service`) is created per project, running the user-specified start command.
- **Port allocation**: `9000 + project_id`. The app must listen on `127.0.0.1:$PORT`.
- **Subdomain routing**: Cloudflare wildcards `*.justralph.it` to nginx, which sets the `X-Subdomain` header. FastAPI middleware reads the header and reverse-proxies the request to the project's port.
- Deployment is triggered automatically when Ralph finishes all issues (if `deploy_type` is configured), or manually via the API.

## Data

| What | Where |
|------|-------|
| App database (users, projects) | `./data/jri.db` (SQLite) |
| Project repos + uploads | `./data/<github-username>/<project-name>/` |
| Ralphy session files | `~/.claude/projects/-home-nico-jri-data-<user>-<project>/` |
| Auth credentials | `./.env`, `~/.config/gh/hosts.yml`, `~/.claude/.credentials.json` |
| Waitlist (maintenance mode) | `./data/waitlist.txt` |

## Logs

All application logs are written to the `logs/` directory (gitignored):

```
logs/
  app.log                              # FastAPI/uvicorn app logs (rotating, 5 MB)
  projects/<project-name>/ralph.log    # Ralph loop stdout per project
  projects/<project-name>/ralphy.log   # Ralphy chat output per project
```

Console output is preserved for development. Directories are created automatically.

Other log sources:

| What | Where |
|------|-------|
| systemd service | `journalctl -u jri -f` |
| Ralphy session files | `~/.claude/projects/-home-nico-jri-data-<user>-<project>/<session-id>.jsonl` |
| nginx access/error | `/var/log/nginx/access.log`, `/var/log/nginx/error.log` |
