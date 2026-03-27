---
title: Privacy & Security
description: How we protect your data and ensure security
nav_label: Privacy & Security
order: 5
---

# Privacy & Security

Your security and privacy are our top priorities.

## Data Protection

### What We Store

- GitHub profile information (username, email)
- Project metadata and configuration
- Generated code repositories

### What We Don't Store

- Your GitHub password (we use OAuth)
- Payment card details (handled by Stripe)
- Sensitive secrets in plain text

## Security Measures

### Authentication

- GitHub OAuth for secure login
- Signed session tokens with `itsdangerous`
- 30-day session expiry

### Data Isolation

- Each project has its own directory
- User data is separated by GitHub username
- No cross-user data access

### Code Security

- HTML escaping before markdown rendering
- Path traversal protection
- File upload size limits (10 MB max)

## Privacy Policy

We only collect data necessary for the service:

- To authenticate you
- To create and manage your projects
- To process payments

We never sell or share your data with third parties.

## Reporting Issues

Found a security vulnerability? Email us at security@justralph.it