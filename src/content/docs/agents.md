---
title: AI Agents
description: Learn about Ralphy and Ralph, our AI-powered agents
nav_label: Agents
order: 2
---

# AI Agents

Just Ralph It uses two specialized AI agents to build your projects.

## Ralphy (Interviewer)

Ralphy is your project interviewer. It:

- Asks clarifying questions about your project
- Understands your requirements and constraints
- Creates detailed, actionable issues
- Breaks down complex features into manageable tasks

## Ralph (Builder)

Ralph is your implementation agent. It:

- Picks up open issues one by one
- Implements features using test-driven development
- Writes clean, well-documented code
- Runs tests to verify implementations

## Model Configuration

By default, both agents use `opencode-go/glm-5` (free tier). You can override this with environment variables:

| Env Var | Default | Paid Example |
|---------|---------|--------------|
| `RALPH_MODEL` | `opencode-go/glm-5` | `opencode/gpt-5.4` |
| `RALPHY_MODEL` | `opencode-go/glm-5` | `opencode/glm-5` |