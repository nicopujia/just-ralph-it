---
title: Project Foundation
priority: 0
created: '2026-03-21'
acceptance_criteria:
- Project runs with 'uvicorn app.main:app' from ~/jri/, serves on 127.0.0.1:8000,
  and has a working SQLite database at ~/jri/data/jri.db with users and projects tables.
---

Set up the foundational project structure, dependencies, configuration, and database schema that all other features build upon.
