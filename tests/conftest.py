"""Pytest configuration and shared fixtures for JRI tests.

Provides:
- Test database seeding with admin user
- Uvicorn server fixture (auto-starts for tests)
- Admin user lookup
- Project cleanup

Requires:
- gh CLI authenticated as JRI_TEST_GITHUB_USER (default: ralphpujia)
"""

import multiprocessing
import os
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing

import httpx
import pytest

# Add src/ to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app.auth_utils import create_session_token
from app.config import DATA_DIR
from app.main import app

# Configurable test GitHub account (env var or default)
TEST_GITHUB_USER = os.environ.get("JRI_TEST_GITHUB_USER", "ralphpujia")


def _check_gh_auth() -> None:
    """Verify gh CLI is authenticated as the test user. Exit if not."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = result.stdout + result.stderr

        if f"account {TEST_GITHUB_USER}" not in output.lower():
            print(
                f"\n{'=' * 60}\n"
                f"ERROR: gh CLI must be logged in as '{TEST_GITHUB_USER}'\n"
                f"Current auth:\n{output}\n"
                f"Run: gh auth login\n"
                f"Or set JRI_TEST_GITHUB_USER env var to match your gh account\n"
                f"{'=' * 60}\n"
            )
            sys.exit(1)

    except FileNotFoundError:
        print("\nERROR: gh CLI not found. Install it: https://cli.github.com/\n")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("\nERROR: gh auth status timed out\n")
        sys.exit(1)


# Check gh auth at module load (before any tests run)
_check_gh_auth()


def _init_test_db() -> None:
    """Initialize the database with schema (sync version of init_db)."""
    import asyncio

    from app.database import init_db

    asyncio.run(init_db())


def _ensure_test_admin_exists() -> int:
    """Ensure ralphpujia exists as admin in the database. Returns user id."""
    db_path = DATA_DIR / "jri.db"

    # Initialize the database schema first
    _init_test_db()

    conn = sqlite3.connect(db_path)
    try:
        # Check if user already exists
        cursor = conn.execute(
            "SELECT id, role FROM users WHERE github_username = ?",
            (TEST_GITHUB_USER,),
        )
        row = cursor.fetchone()
        if row:
            user_id, role = row
            # Ensure role is admin for testing
            if role != "admin":
                conn.execute(
                    "UPDATE users SET role = 'admin' WHERE id = ?",
                    (user_id,),
                )
                conn.commit()
            return user_id

        # Create user with a unique github_id based on username hash
        github_id = abs(hash(TEST_GITHUB_USER)) % 10**9
        conn.execute(
            """
            INSERT INTO users (github_id, github_username, github_token, role)
            VALUES (?, ?, ?, ?)
            """,
            (github_id, TEST_GITHUB_USER, "test-token-unused", "admin"),
        )
        conn.commit()

        # Get the inserted user id
        cursor = conn.execute(
            "SELECT id FROM users WHERE github_username = ?",
            (TEST_GITHUB_USER,),
        )
        return cursor.fetchone()[0]
    finally:
        conn.close()


def _find_free_port() -> int:
    """Find an available port."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _run_server(port: int) -> None:
    """Run uvicorn server in a subprocess."""
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


@pytest.fixture(scope="session")
def test_server():
    """Start a uvicorn server for the test session.

    Yields the base URL (e.g., http://127.0.0.1:12345).
    Server is automatically stopped after all tests complete.
    """
    # Ensure test admin user exists before starting server
    _ensure_test_admin_exists()

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    # Start server in a separate process
    proc = multiprocessing.Process(target=_run_server, args=(port,), daemon=True)
    proc.start()

    # Wait for server to be ready
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=1) as c:
                resp = c.get(f"{base_url}/")
                if resp.status_code in (200, 302, 307):
                    break
        except (httpx.ConnectError, httpx.ReadTimeout):
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError(f"Server failed to start on {base_url}")

    yield base_url

    proc.terminate()
    proc.join(timeout=5)


@pytest.fixture(scope="session")
def admin_user(test_server):
    """Find the test admin user by querying /auth/me.

    Returns dict with 'id', 'github_username', 'role', etc.
    Skips if user doesn't exist.
    """
    base_url = test_server

    for uid in range(1, 51):
        token = create_session_token(uid)
        try:
            with httpx.Client(
                base_url=base_url, cookies={"session": token}, timeout=5
            ) as c:
                resp = c.get("/auth/me")
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("github_username") == TEST_GITHUB_USER:
                        return {"id": uid, **data}
        except Exception:
            continue

    pytest.skip(f"{TEST_GITHUB_USER} user not found in database (checked IDs 1-50)")


@pytest.fixture
def api_client(test_server, admin_user):
    """HTTP client authenticated as admin user."""
    token = create_session_token(admin_user["id"])
    with httpx.Client(
        base_url=test_server,
        cookies={"session": token},
        timeout=30,
    ) as client:
        yield client


@pytest.fixture
def anon_client(test_server):
    """HTTP client with no authentication."""
    with httpx.Client(base_url=test_server, timeout=30) as client:
        yield client


# Track created projects for cleanup
_created_projects: set[tuple[str, int]] = set()


def register_project(name: str, user_id: int) -> None:
    """Register a project for cleanup after test."""
    _created_projects.add((name, user_id))


def unregister_project(name: str, user_id: int) -> None:
    """Unregister a project (already cleaned up)."""
    _created_projects.discard((name, user_id))


@pytest.fixture(autouse=True)
def cleanup_test_projects(test_server, admin_user):
    """Clean up any projects created during tests."""
    yield

    # Clean up any registered projects
    for name, user_id in list(_created_projects):
        token = create_session_token(user_id)
        try:
            with httpx.Client(
                base_url=test_server, cookies={"session": token}, timeout=30
            ) as c:
                # Stop ralph first
                c.post(f"/api/projects/{name}/ralph/stop")
                time.sleep(1)
                # Delete project
                for _ in range(3):
                    resp = c.delete(f"/api/projects/{name}")
                    if resp.status_code != 409:
                        break
                    time.sleep(1)
        except Exception:
            pass
        _created_projects.discard((name, user_id))
