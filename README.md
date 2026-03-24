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
                    |         Claude CLI     Claude CLI
                    |             |              |
                    +-------> Beads (bd) <------+
                              Dolt server
```

- **FastAPI** app with Jinja2 templates, SSE for real-time streaming
- **SQLite** for app metadata (users, projects, sessions)
- **Beads (`bd`)** for issue tracking, backed by a shared Dolt SQL server
- **Ralphy**: interviews users, creates detailed issues (Claude Opus via CLI)
- **Ralph**: picks up open issues one by one, implements via TDD (Claude Opus via CLI)
- **GitHub**: OAuth login + `ralphpujia` bot account creates repos per project
- **Stripe**: per-project payments
- **nginx + Cloudflare**: reverse proxy, SSL, subdomain routing for deployed projects

## Local setup

```bash
# 1. Clone
git clone https://github.com/ralphpujia/jri.git && cd jri

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. External tools (must be on PATH)
#    - claude (Anthropic CLI)
#    - bd (beads issue tracker)
#    - gh (GitHub CLI, authenticated as the bot account)

# 4. Configure environment
cp example.env .env
# Edit .env with your credentials

# 5. Run
make run
# App starts at http://127.0.0.1:8000
```

## Data

| What | Where |
|------|-------|
| App database (users, projects) | `./data/jri.db` (SQLite) |
| Project repos + uploads | `./data/<github-username>/<project-name>/` |
| Beads issues (per project) | `./data/<user>/<project>/.beads/` |
| Shared Dolt server | `~/.beads/shared-server/` |
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

## Environment variables

See `example.env`. Required: `SECRET_KEY`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`.
