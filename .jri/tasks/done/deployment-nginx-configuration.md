---
title: Deployment & Nginx Configuration
priority: 0
created: '2026-03-21'
acceptance_criteria:
- App is accessible at https://justralph.it, served by uvicorn behind nginx. Existing
  static page serves as fallback for unmatched routes. SSE streaming works through
  nginx (proper proxy headers). The app starts automatically on server boot.
---

Deploy the app behind nginx on justralph.it, with the static page as fallback.
