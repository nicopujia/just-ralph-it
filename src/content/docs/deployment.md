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

## Subdomain Routing

Each project gets a unique subdomain:

```
{project-name}.{username}.justralph.it
```

Example: `myapp.johndoe.justralph.it`

## Manual Deployment

You can also trigger deployment manually via the API.

## Requirements

Your project must:

- Listen on `127.0.0.1:$PORT` (provided via environment)
- Have a valid start command configured
- Not require root privileges

## Logs

Deployment logs are available in your project's `.jri/logs/` directory.