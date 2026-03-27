---
title: Deployment
description: How projects are deployed and accessed
nav_label: Deployment
order: 6
---

# Deployment

Learn how your projects are deployed and made accessible.

## Automatic Deployment

When Ralph finishes all issues, your project can be automatically deployed (if configured).

## Deployment Architecture

```
User Request
    ↓
Cloudflare (*.justralph.it)
    ↓
nginx (port 80)
    ↓
FastAPI Reverse Proxy
    ↓
Your Project (port 9000 + project_id)
```

## Subdomain Routing

Each project gets a unique subdomain:

```
{project-name}.{username}.justralph.it
```

Example: `myapp.johndoe.justralph.it`

## Systemd Services

Each deployed project runs as a systemd service:

- Service name: `jri-deploy-{project-name}.service`
- Port allocation: `9000 + project_id`
- Automatic restart on failure

## Manual Deployment

You can also trigger deployment manually via the API.

## Requirements

Your project must:

- Listen on `127.0.0.1:$PORT` (provided via environment)
- Have a valid start command configured
- Not require root privileges

## Logs

Deployment logs are available in your project's `.jri/logs/` directory.