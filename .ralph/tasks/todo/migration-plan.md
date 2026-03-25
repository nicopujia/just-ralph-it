# Migration Plan: IONOS → OVH

## Context
Migrating justralph.it from current IONOS VPS (3.8GB RAM, Ubuntu 24.04) to OVH VPS (8GB RAM target).

## Pre-migration

1. Enable maintenance mode: set `MAINTENANCE_MODE=true` in `.env`, restart JRI
2. Wait for any running Ralph loops to finish: `sqlite3 data/jri.db "SELECT name, ralph_loop_status FROM projects WHERE ralph_loop_status = 'running';"`
3. Stop JRI: `sudo systemctl stop jri`

## Run the migration script

```bash
./deploy/setup.sh <new-server-ip>
```

This handles:
- User creation, system packages, external tools (node, gh, claude, bd, mosh)
- Repo clone + Python deps
- Credential copy (.env, gh token, claude credentials)
- Data copy (SQLite DB, project repos, beads data, Claude sessions)
- Systemd + nginx setup
- Service start

## Post-migration on new server

1. SSH in and verify: `curl -s http://127.0.0.1:8000/ | head -5`
2. Start shared Dolt server: `bd dolt start` (or let JRI startup handle it)
3. Verify projects load: `curl -s http://127.0.0.1:8000/api/projects` (with auth cookie)

## DNS cutover

1. In Cloudflare dashboard, update A records:
   - `justralph.it` → new IP
   - `*.justralph.it` → new IP
2. TTL is typically 5 min with Cloudflare proxying — cutover is near-instant

## Post-cutover verification

1. Test login flow (GitHub OAuth)
2. Test an existing project loads with chat history
3. Test Ralphy responds to a message
4. Disable maintenance mode: remove `MAINTENANCE_MODE=true` from `.env`, restart JRI

## Rollback

If something breaks:
1. Point Cloudflare DNS back to old IONOS IP
2. Re-enable JRI on old server: `sudo systemctl start jri`

Keep the old server running for at least 48h after cutover.

## Data that must be migrated

| What | Path | Method |
|------|------|--------|
| App database | `~/jri/data/jri.db` | scp |
| Project repos + uploads | `~/jri/data/*/` | rsync |
| Beads shared server | `~/.beads/` | rsync |
| Claude sessions | `~/.claude/projects/` | rsync |
| GitHub CLI auth | `~/.config/gh/hosts.yml` | scp |
| Claude CLI auth | `~/.claude/.credentials.json` | scp |
| App secrets | `~/jri/.env` | scp |
