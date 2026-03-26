import logging
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (~/jri/.env)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# GitHub OAuth
GITHUB_CLIENT_ID: str = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET: str = os.environ.get("GITHUB_CLIENT_SECRET", "")

# App secret key (for signing sessions/tokens)
SECRET_KEY: str = os.environ.get("SECRET_KEY", "")

# Staging mode — uses Stripe test credentials
STAGING: bool = os.getenv("STAGING", "").lower() in ("1", "true", "yes")

# Stripe — use test or prod keys based on STAGING
if STAGING:
    STRIPE_SECRET_KEY: str = os.environ.get("STRIPE_SECRET_KEY", "")
else:
    STRIPE_SECRET_KEY: str = os.environ.get(
        "PROD_STRIPE_SECRET_KEY", os.environ.get("STRIPE_SECRET_KEY", "")
    )

# Base URL (used for OAuth callbacks, Stripe redirects, etc.)
BASE_URL: str = os.environ.get("BASE_URL", "https://justralph.it")

# Data directory for persistent storage
DATA_DIR: Path = Path.home() / "jri" / "data"


# Ralph bot GitHub token – read from gh CLI at import time and cached
def _get_ralph_bot_github_token() -> str:
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


RALPH_BOT_GITHUB_TOKEN: str = _get_ralph_bot_github_token()

# ── Startup validation ──
_REQUIRED = {
    "GITHUB_CLIENT_ID": GITHUB_CLIENT_ID,
    "GITHUB_CLIENT_SECRET": GITHUB_CLIENT_SECRET,
    "SECRET_KEY": SECRET_KEY,
}
_missing = [name for name, val in _REQUIRED.items() if not val]
if _missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(_missing)}")

if not STRIPE_SECRET_KEY:
    logging.getLogger(__name__).warning(
        "STRIPE_SECRET_KEY not set — Stripe payments will not work"
    )

# Pricing (in cents)
PRICE_PROJECT_BASE = 1000  # $10 one-time base fee per project
PRICE_PER_TASK = 500  # $5 per task
MAX_FREE_PROJECTS = 3  # free projects before subscription required
PRICE_PRO_MONTHLY = 2000  # $20/mo for unlimited projects
