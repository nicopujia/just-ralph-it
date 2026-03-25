---
title: Initialize Python project with FastAPI, uvicorn, and project structure
priority: 0
assignee: ralph
created: '2026-03-21'
acceptance_criteria:
- Running `pip install -r requirements.txt` succeeds with no errors.
- Running `cd ~/jri && python -c 'from app.main import app; print(app.title)'` prints
  'Just Ralph It'.
- Running `cd ~/jri && python -c 'from app.config import GITHUB_CLIENT_ID, SECRET_KEY,
  DATA_DIR; print(GITHUB_CLIENT_ID); print(DATA_DIR)'` prints the values from ~/jri.env
  and the correct data directory path.
- All files listed in the directory structure above exist.
- requirements.txt contains exactly the packages listed above (versions may be adjusted
  if needed for compatibility, but all packages must be present).
- Every .py file is valid Python (no syntax errors).
---

Create the Python project skeleton for JRI.

## What to create

Directory structure:
```
~/jri/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI app, CORS, lifespan, static mount
│   ├── config.py         # Load env vars from ~/jri.env using python-dotenv
│   ├── database.py       # SQLite connection via aiosqlite
│   ├── models.py         # Pydantic models for API request/response
│   └── routers/
│       ├── __init__.py
│       ├── auth.py       # GitHub OAuth endpoints
│       ├── projects.py   # Project CRUD endpoints
│       ├── chat.py       # Ralphy chat endpoints
│       ├── ralph.py      # Ralph loop control endpoints
│       ├── uploads.py    # File upload management endpoints
│       └── sse.py        # SSE streaming endpoints
├── static/
│   └── (empty for now, will hold frontend assets)
├── templates/
│   └── (empty for now, will hold Jinja2 templates)
├── requirements.txt
├── NOTES.md
├── CLAUDE.md
└── README.md
```

## requirements.txt contents
```
fastapi==0.115.0
uvicorn[standard]==0.30.0
aiosqlite==0.20.0
python-dotenv==1.0.1
httpx==0.27.0
itsdangerous==2.2.0
python-multipart==0.0.9
jinja2==3.1.4
markdown==3.7
stripe==10.0.0
```

## app/config.py behavior
- Load all env vars from ~/jri.env using python-dotenv
- Expose them as module-level constants: GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, SECRET_KEY, STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY
- Also expose: DATA_DIR = Path.home() / 'jri' / 'data', RALPH_BOT_GITHUB_TOKEN (read from `gh auth token --hostname github.com` subprocess at startup, cached)

## app/main.py behavior
- Create FastAPI app with title 'Just Ralph It'
- On startup lifespan: ensure DATA_DIR exists, initialize database
- Mount static files at /static
- Include all routers
- No CORS needed (same-origin, served behind nginx)
