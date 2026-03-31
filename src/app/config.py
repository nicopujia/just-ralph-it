import logging
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Maintenance mode — blocks user access to projects
MAINTENANCE_MODE: bool = os.getenv("MAINTENANCE_MODE", "").lower() in (
    "1",
    "true",
    "yes",
)

# Mode-based config (DEV or PROD)
MODE: str = os.environ.get("MODE", "PROD").upper()
assert MODE in ("DEV", "PROD"), f"MODE must be DEV or PROD, got: {MODE}"
_prefix = f"{MODE}_"

SECRET_KEY: str = os.environ.get("SECRET_KEY", "dev")
if SECRET_KEY == "dev" and MODE == "PROD":
    logging.getLogger(__name__).warning(
        "SECRET_KEY not set — using insecure default in PROD mode"
    )

GITHUB_CLIENT_ID: str = os.environ.get(f"{_prefix}GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET: str = os.environ.get(f"{_prefix}GITHUB_CLIENT_SECRET", "")
STRIPE_SECRET_KEY: str = os.environ.get(f"{_prefix}STRIPE_SECRET_KEY", "")
BASE_URL: str = os.environ.get(f"{_prefix}BASE_URL", "http://localhost:8000")

# Data directory for persistent storage (<project_root>/.data)
DATA_DIR: Path = Path(__file__).resolve().parent.parent.parent / ".data"


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
}
_missing = [name for name, val in _REQUIRED.items() if not val]
if _missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(_missing)}")

if not STRIPE_SECRET_KEY:
    logging.getLogger(__name__).warning(
        "STRIPE_SECRET_KEY not set — Stripe payments will not work"
    )

# Pricing (in cents)
PRICE_PER_TASK = 400  # $4 per task
MAX_FREE_PROJECTS = 3  # free projects before subscription required
PRICE_PRO_MONTHLY = 2000  # $20/mo for unlimited projects + VPS

# Agent models
RALPH_MODEL: str = "openai/gpt-5.4-mini"
RALPHY_MODEL: str = "openai/gpt-5.4"
