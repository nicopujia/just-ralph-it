---
title: JRI CLI Package
depends_on:
  - migrate-to-opencode
---

## Context

JRI needs a public pip-installable CLI (`jri`) that orchestrates the Ralph loop. The CLI is a thin wrapper around OpenCode's server-client architecture. Paid users get a dedicated VPS (one per user, shared across their projects) with full root access. Free-tier users run on the central justralph.it server (no VPS provisioning).

## Two deployment modes

### Free tier (central server)
- Projects live on justralph.it alongside the web app
- Ralph loop runs as a local OpenCode server process on the same machine
- No VPS provisioning, no CLI needed
- Same OpenCode API, just accessed locally

### Paid tier (per-user VPS)
- Main server auto-provisions one VPS per user via cloud API (Hetzner/DO)
- Single `opencode serve` instance handles all of the user's projects (separate sessions)
- `jri` CLI orchestrates per-project: pick issue, send prompt via OpenCode API, check result, next issue
- Multiple projects can run concurrently on the same VPS
- OpenCode plugin streams events back to justralph.it for live UI updates
- User gets full root access to their VPS

## Architecture

**Single repo, two build targets:**
- `src/jri/` -- public package published to PyPI (CLI + loop orchestrator)
- `src/app/` -- private, runs only on main server (web app, prompts, billing)

The public package is a "dumb controller": it receives a prompt and repo URL, orchestrates the loop via OpenCode's API, and streams output back. No proprietary logic.

**Models (via OpenCode Zen):**
- Ralph (coding loop): `opencode/gpt-5.4` (Hephaestus agent from OMO)
- Ralphy (chat/PRD): `opencode/glm-5`

### How it works (VPS mode)

1. Main server provisions one VPS per user, installs `jri` + `opencode`
2. VPS runs a single `opencode serve` (headless, port 4096, permissions all-allow)
3. `jri run --project <name>` starts a loop for one project:
   - Fetches prompt + config from justralph.it
   - Picks next ready issue from `.jri/tasks/todo/`
   - Creates an OpenCode session for this project
   - Sends prompt via `session.prompt()` API
   - Monitors session events via SSE (`/event` endpoint)
   - On completion: git push, mark issue done, pick next
4. Multiple `jri run` processes can run concurrently for different projects (same OpenCode server, different sessions)
5. OpenCode plugin forwards events to justralph.it for live UI
6. Loop repeats until all issues done or stopped

### How it works (central server mode)

Same logic, but `opencode serve` runs locally on justralph.it. The web app calls the same orchestration code directly (no CLI, no network hop). Events go straight to the SSE bus.

## Package Structure

```
src/
  jri/                          # PUBLIC -- published to PyPI
    __init__.py
    cli/
      __init__.py
      main.py                   # click entrypoint
      commands/
        init.py                 # jri init
        run.py                  # jri run
        stop.py                 # jri stop
        status.py               # jri status
        logs.py                 # jri logs
        update.py               # jri update
    core/
      __init__.py
      loop.py                   # ralph loop orchestrator (uses OpenCode API)
      tasks.py                  # YAML task tracking
      config.py                 # CLI config (~/.config/jri/config.toml)
      logging_config.py         # logging setup
      opencode_client.py        # OpenCode server API client (sessions, prompts, events)
      auto_update.py            # periodic self-update via pip
    opencode_plugin/
      __init__.py
      relay.py                  # plugin that forwards OpenCode events to justralph.it

  app/                          # PRIVATE -- stays on main server only
    __init__.py
    main.py                     # FastAPI app
    config.py                   # web config (env vars, secrets)
    database.py
    sse_bus.py
    auth_utils.py
    deploy_manager.py
    models.py
    prompts/                    # proprietary prompts
      ralph.py
      ralphy.py
    routers/
      auth.py
      chat.py
      deploy_proxy.py
      pages.py
      projects.py
      ralph.py
      sse.py
      uploads.py
      agents.py                 # NEW -- receives relayed events from VPS OpenCode plugins
```

## Key Design Decisions

### 1. OpenCode as the runtime

Instead of subprocessing `claude` CLI, we use OpenCode's server API:
- `opencode serve` runs headless on VPS (or locally for free tier)
- `session.prompt()` sends prompts programmatically
- SSE event stream (`/event`) provides real-time output
- Permissions set to `"*": "allow"` for fully non-interactive use
- AGENTS.md / CLAUDE.md compat means existing project rules work

### 2. OpenCode plugin for event relay

An OpenCode plugin (shipped in the `jri` package) hooks into session events and forwards them to justralph.it:
- `session.idle` -- loop iteration complete
- `session.status` -- status changes
- `message.updated` / `message.part.updated` -- live output streaming
- `tool.execute.after` -- tool usage events

The plugin POSTs events to `https://justralph.it/api/agents/{project_id}/event` (or uses a persistent connection).

### 3. Prompt delivery

CLI does NOT contain prompts. On `jri run`, the orchestrator fetches the system prompt from justralph.it. This is then passed to OpenCode via `session.prompt()` or injected via `noReply: true` context message. Keeps prompts private, allows hot-updating.

### 4. GitHub App for per-repo scoping

Users install a GitHub App on their repo. The VPS gets short-lived installation tokens scoped per-repo. Even though multiple projects share one VPS, each project's token can only access its own repo. No shared bot account on VPSes. The `ralphpujia` account stays on the central server only.

### 5. Auto-update

Background task checks PyPI every 15 min. When newer version found, waits for current issue to finish, runs `pip install --upgrade jri`, restarts via systemd.

## CLI Commands

| Command | Description |
|---------|-------------|
| `jri init --token <api-token>` | Register VPS with main server, start opencode serve |
| `jri run --project <name>` | Fetch prompt, start Ralph loop for a project via OpenCode API |
| `jri stop` | Graceful shutdown after current issue |
| `jri status` | Show current loop state (queries OpenCode session status) |
| `jri logs [--follow]` | Stream OpenCode session output |
| `jri update` | `pip install --upgrade jri` + restart systemd service |

## Implementation Phases

### Phase 1: Migrate to OpenCode (separate task)
Prerequisite. Replace `claude` CLI with OpenCode in the existing codebase.

### Phase 2: Extract loop orchestrator
1. Refactor `ralph_loop.py` to use OpenCode API instead of subprocess
2. Extract core loop logic into `src/jri/core/loop.py`
3. Central server uses it directly; CLI wraps it
4. Move `tasks.py` to `src/jri/core/tasks.py`

### Phase 3: Build CLI
5. Create `src/jri/cli/` with Click commands
6. Add `[project.scripts]` to pyproject.toml
7. Test locally: `pip install -e .` then `jri run`

### Phase 4: OpenCode event relay plugin
8. Create `src/jri/opencode_plugin/relay.py`
9. Add `/api/agents/{project_id}/event` endpoint to web app
10. Wire up web UI to display relayed events

### Phase 5: GitHub App
11. Create GitHub App on github.com
12. Add installation flow to web app
13. VPS receives scoped installation token during `jri init`

### Phase 6: Auto-provisioning + publishing
14. Add cloud provider API integration (Hetzner/DO) to web app
15. Create provisioning flow: spin up one VPS per user, install jri + opencode, run init
16. Publish `jri` to PyPI
17. Implement auto-update in CLI
18. Create systemd unit template for VPS (one opencode serve + N jri run processes)
