---
title: Make Stripe checkout URLs configurable instead of hardcoded
priority: 1
assignee: Nicolás Pujia
created: '2026-03-21'
acceptance_criteria:
- BASE_URL is loaded from environment variable with default https://justralph.it
- Stripe checkout URLs use BASE_URL
- OAuth callback URL uses BASE_URL
- No hardcoded justralph.it URLs remain in Python source files
---

In app/routers/ralph.py lines 100-101, the Stripe checkout success_url and cancel_url are hardcoded to https://justralph.it. This breaks development environments.

WHAT TO CHANGE:

1. In app/config.py, add:
   BASE_URL = os.getenv('BASE_URL', 'https://justralph.it')

2. In app/routers/ralph.py:
   - Add import: from app.config import BASE_URL (alongside existing imports from app.config)
   - Change line 100: success_url=f'{BASE_URL}/project/{name}?payment=success&session_id={{CHECKOUT_SESSION_ID}}'
   - Change line 101: cancel_url=f'{BASE_URL}/project/{name}?payment=cancel'

3. Similarly in app/routers/auth.py line 17:
   - Change _CALLBACK_URI = 'https://justralph.it/auth/callback'
   - TO: from app.config import BASE_URL then _CALLBACK_URI = f'{BASE_URL}/auth/callback'
