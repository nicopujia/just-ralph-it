# JRI CLI Package

## Context

JRI runs Ralph loops on per-project VPSes with full root access. We need a public pip-installable CLI (`jri`) that controls the Ralph loop on each VPS. The web app, prompts, and business logic stay private in the same repo. The main server auto-provisions VPSes, sends prompts at runtime, and receives live output via WebSocket. GitHub access is scoped per-repo via a GitHub App.

## Architecture

**Single repo, two build targets:**
- `src/jri/` -- public package published to PyPI (CLI + loop controller)
- `src/app/` -- private, runs only on main server (web app, prompts, billing)

The public package is a "dumb controller": it receives a prompt and repo URL, runs Claude in a loop, and streams output back. No proprietary logic.

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
      ralph_loop.py             # loop controller (refactored, no DB/SSE deps)
      tasks.py                  # YAML task tracking
      config.py                 # CLI config (~/.config/jri/config.toml)
      logging_config.py         # logging setup
      ws_client.py              # WebSocket client -- streams output to main server
      auto_update.py            # periodic self-update via pip

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
      agents.py                 # NEW -- WebSocket endpoint for VPS connections
```

## Key Design Decisions

### 1. Ralph loop decoupling

Refactor current `ralph_loop.py` to a hooks pattern:

```python
class RalphLoopHooks(Protocol):
    async def on_status_change(self, status: str) -> None: ...
    async def on_output_line(self, line: str) -> None: ...
    async def on_issue_event(self, event: str, data: dict) -> None: ...
    async def on_loop_idle(self) -> None: ...
```

- **CLI provides:** `CLIHooks` -- streams output via WebSocket, writes status to local `.jri_state`
- **Web app provides:** `WebHooks` -- writes to DB, publishes to SSE bus

### 2. Prompt delivery

CLI does NOT contain prompts. On `jri run`, the VPS connects to justralph.it via WebSocket. The main server sends the system prompt as part of the initial handshake. Keeps prompts private, allows hot-updating without package upgrades.

### 3. GitHub App for per-repo scoping

Users install a GitHub App on their repo. Each VPS gets a short-lived installation token scoped to only its repo. No shared bot account exposure.

### 4. VPS-to-server communication (WebSocket)

Persistent WebSocket to `wss://justralph.it/ws/agent/{project_id}`.

**VPS -> Server:** Ralph stdout lines, status changes, issue events
**Server -> VPS:** System prompt, stop/pause commands, config updates

### 5. Auto-update

Background task checks PyPI every 15 min. When newer version found, waits for current issue to finish, runs `pip install --upgrade jri`, restarts via systemd.

## CLI Commands

| Command | Description |
|---------|-------------|
| `jri init --project-id <id> --token <api-token>` | Register with main server, clone repo, set up .jri structure |
| `jri run` | Connect to main server via WS, receive prompt, start Ralph loop |
| `jri stop` | Graceful shutdown after current issue |
| `jri status` | Show current loop state from `.jri_state` |
| `jri logs [--follow]` | Tail `.jri/logs/ralph.log` |
| `jri update` | `pip install --upgrade jri` + restart systemd service |

## pyproject.toml Changes

```toml
[project]
name = "jri"
version = "0.2.0"
description = "Ralph loop controller for Just Ralph It"
requires-python = ">=3.12"
dependencies = [
    "click>=8.0",
    "websockets>=12.0",
    "httpx>=0.27",
    "pyyaml>=6.0",
    "rich>=13.0",
]

[project.scripts]
jri = "jri.cli.main:cli"

[dependency-groups]
dev = ["pytest", "ruff", "ty>=0.0.25"]
web = [
    "fastapi==0.115.0",
    "uvicorn[standard]==0.30.0",
    "aiosqlite==0.20.0",
    "python-dotenv==1.0.1",
    "httpx==0.27.0",
    "itsdangerous==2.2.0",
    "python-multipart==0.0.9",
    "jinja2==3.1.4",
    "markdown==3.7",
    "stripe==10.0.0",
]
```

## Implementation Phases

### Phase 1: Restructure (no behavior change)
1. Extract shared code (`tasks.py`, `logging_config.py`) into `src/jri/core/`
2. Refactor `ralph_loop.py` into hooks pattern, core logic to `src/jri/core/ralph_loop.py`
3. Web app imports `jri.core.ralph_loop` and provides `WebHooks`
4. Update imports, verify `ty check` and app still works

### Phase 2: Build CLI
5. Create `src/jri/cli/` with Click commands
6. Create `CLIHooks` (local file output, no WS yet)
7. Add `[project.scripts]` to pyproject.toml
8. Test locally: `pip install -e .` then `jri run`

### Phase 3: WebSocket communication
9. Create `src/jri/core/ws_client.py`
10. Create `src/app/routers/agents.py` with WS endpoint
11. Update `CLIHooks` to stream via WebSocket
12. Update web SSE to read from WS-connected agents

### Phase 4: GitHub App
13. Create GitHub App on github.com
14. Add installation flow to web app
15. VPS receives scoped installation token during `jri init`

### Phase 5: Auto-provisioning + publishing
16. Add cloud provider API integration (Hetzner/DO) to web app
17. Create provisioning flow: spin up VPS, install jri, run init
18. Publish `jri` to PyPI
19. Implement auto-update in CLI
20. Create systemd unit template for VPS
