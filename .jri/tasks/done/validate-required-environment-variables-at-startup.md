---
title: Validate required environment variables at startup
priority: 1
assignee: Nicolás Pujia
created: '2026-03-21'
acceptance_criteria:
- App fails to start with clear error message if GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET,
  or SECRET_KEY are missing
- App starts with warning if STRIPE_SECRET_KEY is missing
- Error message lists all missing vars, not just the first one
- Existing .env setup continues to work
---

Currently app/config.py loads env vars with os.getenv() but doesn't validate that required ones are present. If GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, or SECRET_KEY are missing, errors appear at runtime when the feature is first used instead of at startup.

WHAT TO CHANGE in app/config.py:

1. After loading all env vars, add validation for required ones:

   _REQUIRED_VARS = {
       'GITHUB_CLIENT_ID': GITHUB_CLIENT_ID,
       'GITHUB_CLIENT_SECRET': GITHUB_CLIENT_SECRET,
       'SECRET_KEY': SECRET_KEY,
   }

   _missing = [name for name, val in _REQUIRED_VARS.items() if not val]
   if _missing:
       raise RuntimeError(f"Missing required environment variables: {', '.join(_missing)}")

2. Add a warning (not error) for optional but recommended vars:
   if not STRIPE_SECRET_KEY:
       import logging
       logging.getLogger(__name__).warning('STRIPE_SECRET_KEY not set — Stripe payments will not work')

DO NOT add RALPH_BOT_GITHUB_TOKEN to required vars since it's loaded dynamically from gh auth token.
