---
title: Subdomain deployment for user projects
priority: 0
created: '2026-03-21'
acceptance_criteria:
- Projects can be deployed to {name}.justralph.it, nginx routes subdomains, systemd
  manages dynamic app processes, static sites served directly
---

Support deploying web apps built by Ralph on justralph.it subdomains. Each project gets {project_name}.justralph.it. Supports both static sites (served by nginx) and dynamic apps (managed by systemd units with allocated ports).
