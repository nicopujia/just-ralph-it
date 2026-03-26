---
title: Migrate from Claude Code to OpenCode
---

Replace Claude Code CLI with OpenCode as the agent runtime in the Ralph loop.

## Why

OpenCode has a server-client architecture. This means:
- A persistent server process manages sessions and state
- Clients connect to control/observe the agent
- Multiple clients can attach to the same session (like tmux)
- The JRI web UI and CLI can both connect to the same OpenCode server instance
- Live streaming, stop/pause, and session resumption come for free

This is a natural fit for VPS-based Ralph loops: the OpenCode server runs on the VPS, and the JRI CLI + web UI are just clients.

## Scope

- Replace `claude` CLI invocation in `ralph_loop.py` with OpenCode server/client
- Update `deploy/setup.sh` to install OpenCode instead of (or alongside) Claude Code
- Update Ralph system prompt delivery to use OpenCode's API
- Update output streaming to read from OpenCode server instead of parsing subprocess stdout
- Evaluate if OpenCode's session management can replace our `.jri_state` file

## Open Questions

- Does OpenCode support programmatic (non-interactive) usage? API/SDK?
- How does OpenCode handle authentication with Anthropic?
- Can we send a system prompt via the OpenCode API?
- What's the wire protocol for the server-client communication?
- Does it support streaming output to multiple clients simultaneously?
