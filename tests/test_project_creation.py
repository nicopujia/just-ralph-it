"""Integration test for project creation endpoint.

Requires the jri server to be running locally on port 8000.
"""

import os
import sys
import time
import shutil

import httpx

# Add src/ to path so we can import app modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app.auth_utils import create_session_token
from app.config import DATA_DIR, RALPH_BOT_GITHUB_TOKEN

BASE_URL = "http://127.0.0.1:8000"
TEST_PROJECT_NAME = f"jri-test-{int(time.time())}"
TEST_USER_ID = 1  # nicopujia


def main():
    token = create_session_token(TEST_USER_ID)
    cookies = {"session": token}

    client = httpx.Client(base_url=BASE_URL, cookies=cookies, timeout=180)

    try:
        # 1. Create project
        print(f"Creating project '{TEST_PROJECT_NAME}'...")
        resp = client.post(
            "/api/projects",
            json={"name": TEST_PROJECT_NAME, "description": "Automated test project"},
        )
        print(f"  Status: {resp.status_code}")
        if resp.status_code != 200:
            print(f"  Body: {resp.text}")
            sys.exit(1)

        data = resp.json()
        assert data["name"] == TEST_PROJECT_NAME, f"Name mismatch: {data}"
        print("  Project created successfully.")

        # 2. Verify project directory exists
        project_dir = DATA_DIR / "nicopujia" / TEST_PROJECT_NAME
        assert project_dir.is_dir(), f"Project directory missing: {project_dir}"
        print(f"  Directory exists: {project_dir}")

        # 3. Verify GitHub repo exists
        github_repo_name = f"nicopujia-{TEST_PROJECT_NAME}"
        print(f"  Checking GitHub repo 'ralphpujia/{github_repo_name}'...")
        gh_resp = httpx.get(
            f"https://api.github.com/repos/ralphpujia/{github_repo_name}",
            headers={
                "Authorization": f"token {RALPH_BOT_GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )
        assert gh_resp.status_code == 200, (
            f"GitHub repo not found ({gh_resp.status_code}): {gh_resp.text}"
        )
        print("  GitHub repo exists.")

        print("\nAll checks passed!")

    finally:
        # Cleanup: delete the project via API
        print(f"\nCleaning up project '{TEST_PROJECT_NAME}'...")
        del_resp = client.delete(f"/api/projects/{TEST_PROJECT_NAME}")
        if del_resp.status_code == 204:
            print("  Deleted via API.")
        else:
            print(f"  API delete returned {del_resp.status_code}: {del_resp.text}")
            # Manual cleanup fallback
            project_dir = DATA_DIR / "nicopujia" / TEST_PROJECT_NAME
            if project_dir.exists():
                shutil.rmtree(project_dir)
                print("  Removed directory manually.")

        client.close()


if __name__ == "__main__":
    main()
