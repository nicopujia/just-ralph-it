---
title: Security hardening
priority: 0
created: '2026-03-21'
acceptance_criteria:
- All endpoints require auth, rate limiting in place, Stripe URLs configurable, message
  size validated
---

Multiple security vulnerabilities: unauthenticated SSE and Ralph stream endpoints, no rate limiting, hardcoded Stripe URLs, no chat message size limit.
