---
title: Migrate from Claude Code to OpenCode
---

Replace Claude Code CLI with OpenCode as the agent runtime in the Ralph loop.

## Why

OpenCode's server-client architecture gives us:
- **Headless HTTP server** (`opencode serve`) with full REST API
- **Session management** via API (create, prompt, abort, stream)
- **SSE event streaming** for real-time output without subprocess parsing
- **Plugin system** to hook into session/tool events and relay them
- **`opencode attach`** to connect a TUI to a running session (like tmux)
- **`opencode web`** for browser-based access to the same server
- **Permissions system** (`"*": "allow"`) for fully non-interactive use
- **CLAUDE.md compatibility** out of the box (falls back to CLAUDE.md if no AGENTS.md)

## Models (via OpenCode Zen)

- **Ralph** (coding loop): `opencode/gpt-5.4` -- using Hephaestus agent from [OMO](https://github.com/code-yeongyu/oh-my-openagent)
- **Ralphy** (chat/PRD): `opencode/glm-5` (or `opencode-go/glm-5` for free tier)

No direct Anthropic API key needed. All models go through OpenCode Zen.

## What changes

### ralph_loop.py
- Replace `subprocess` Claude CLI invocation with OpenCode HTTP API calls
- Start `opencode serve` in the dev worktree directory
- Create session via `POST /session`
- Configure Ralph agent with Hephaestus-style config (GPT 5.4, autonomous deep worker)
- Send prompt via `POST /session/:id/prompt_async`
- Monitor via SSE `GET /event` -- key events:
  - `message.part.updated` with `type: "text"` for output
  - `message.part.updated` with `type: "step-start"/"step-finish"/"reasoning"` for CoT
  - `session.status` with `type: "busy"/"idle"`
  - `session.idle` when done
  - `session.error` on failure
- Abort via `POST /session/:id/abort`
- Remove `_parse_stream_line()` and stream-json parsing

### prompts/ralph.py
- System prompt delivered via OpenCode agent config (`OPENCODE_CONFIG_CONTENT` env var)
- Agent configured as primary with all tools enabled, permission `"*": "allow"`

### prompts/ralphy.py
- Update to use `opencode/glm-5` model
- (Ralphy chat migration is separate -- focus on Ralph loop first)

### deploy/setup.sh
- OpenCode already installed (v1.2.27)
- Ensure OMO is installed: `opencode plugin add oh-my-openagent` (or local config)
- Remove Claude Code dependency from setup

## OpenCode API reference (verified)

```
POST   /session                    -- create session { title? }
GET    /session/:id                -- get session
POST   /session/:id/prompt_async   -- send message async, returns immediately
POST   /session/:id/message        -- send message, wait for response (blocking)
POST   /session/:id/abort          -- abort running session
DELETE /session/:id                -- delete session
GET    /event                      -- SSE event stream (all events)
GET    /global/health              -- health check { healthy, version }
GET    /agent                      -- list agents
GET    /config/providers           -- list providers and models
```

## SSE event format (verified with GPT 5.4)

```
data: {"type":"message.part.updated","properties":{"sessionID":"...","part":{"type":"text","text":"actual output"}}}
data: {"type":"message.part.updated","properties":{"sessionID":"...","part":{"type":"reasoning","text":"thinking..."}}}
data: {"type":"message.part.updated","properties":{"sessionID":"...","part":{"type":"step-start","text":""}}}
data: {"type":"message.part.updated","properties":{"sessionID":"...","part":{"type":"step-finish","text":""}}}
data: {"type":"session.status","properties":{"sessionID":"...","status":{"type":"busy"}}}
data: {"type":"session.status","properties":{"sessionID":"...","status":{"type":"idle"}}}
data: {"type":"session.idle","properties":{"sessionID":"..."}}
data: {"type":"session.error","properties":{"sessionID":"...","error":{"name":"...","data":{"message":"..."}}}}
data: {"type":"message.updated","properties":{"sessionID":"...","info":{"role":"assistant","modelID":"gpt-5.4","tokens":{...}}}}
```

## Implementation plan

1. Add OpenCode server lifecycle to `RalphLoop` (start/stop `opencode serve`)
2. Replace claude subprocess with OpenCode API calls (httpx)
3. Replace `_stream_process_output()` + `_parse_stream_line()` with SSE event streaming
4. Configure Ralph agent via `OPENCODE_CONFIG_CONTENT` (system prompt + GPT 5.4 + all permissions)
5. Update `stop()` to abort session via API instead of killing subprocess
6. Test end-to-end on a project

## Answered questions

- **One server per project**: yes, `opencode serve` runs in the dev worktree directory (project-scoped)
- **Streaming**: SSE `/event` streams all events including tool outputs and reasoning steps
- **API overhead**: negligible, server runs locally on 127.0.0.1
