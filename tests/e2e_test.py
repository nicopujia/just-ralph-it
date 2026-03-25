"""End-to-end tests for justralph.it using Playwright.

Runs against the production site. Uses a session cookie generated from
auth_utils to bypass GitHub OAuth.
"""

import os
import sys
import time

import pytest
from playwright.sync_api import sync_playwright, Page, BrowserContext
import httpx

# Add src/ to path so we can import app modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app.auth_utils import create_session_token

BASE_URL = "https://justralph.it"
TEST_USER_ID = 1  # nicopujia — must exist in the production database


def get_session_cookie_value() -> str:
    """Generate a valid session cookie for the test user."""
    return create_session_token(TEST_USER_ID)


# ── Helpers ───────────────────────────────────────────────────────────

def delete_project_api(name: str):
    """Delete a project via the API (cleanup helper)."""
    token = get_session_cookie_value()
    with httpx.Client(base_url=BASE_URL, cookies={"session": token}, timeout=30) as client:
        resp = client.delete(f"/api/projects/{name}?delete_repo=true")
        assert resp.status_code in (204, 404), f"Delete failed: {resp.status_code} {resp.text}"


def create_project_api(name: str, description: str = "E2E test project") -> dict:
    """Create a project via the API (setup helper)."""
    token = get_session_cookie_value()
    with httpx.Client(base_url=BASE_URL, cookies={"session": token}, timeout=180) as client:
        resp = client.post("/api/projects", json={"name": name, "description": description})
        assert resp.status_code == 200, f"Create failed: {resp.status_code} {resp.text}"
        return resp.json()


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def anon_page(browser):
    """A page with no session cookie (logged out)."""
    context = browser.new_context()
    page = context.new_page()
    yield page
    page.close()
    context.close()


@pytest.fixture
def page(browser):
    """A page with the session cookie set (logged in)."""
    token = get_session_cookie_value()
    context = browser.new_context()
    context.add_cookies([{
        "name": "session",
        "value": token,
        "domain": "justralph.it",
        "path": "/",
        "httpOnly": False,
        "secure": True,
        "sameSite": "Lax",
    }])
    p = context.new_page()
    yield p
    p.close()
    context.close()


# ── Tests ─────────────────────────────────────────────────────────────

def test_landing_page(anon_page: Page):
    """Visit /, verify title and sign-in button."""
    anon_page.goto(BASE_URL)
    anon_page.wait_for_load_state("domcontentloaded")

    title = anon_page.title()
    assert "Just Ralph It" in title

    sign_in = anon_page.locator("a.btn-github")
    sign_in.wait_for(state="visible", timeout=5000)
    text = sign_in.inner_text()
    assert "Sign in" in text


def test_dashboard_loads(page: Page):
    """Set session cookie, visit /dashboard, verify heading."""
    page.goto(f"{BASE_URL}/dashboard")
    page.wait_for_load_state("domcontentloaded")

    heading = page.locator("h2")
    heading.wait_for(state="visible", timeout=10000)
    text = heading.inner_text()
    assert "Your Projects" in text


def test_create_project(page: Page):
    """Create a project via the UI, verify redirect and Ralphy's first message."""
    project_name = f"e2e-test-{int(time.time())}"

    try:
        page.goto(f"{BASE_URL}/new")
        page.wait_for_load_state("domcontentloaded")

        # Fill in the form
        page.fill("#name", project_name)
        page.fill("#description", "Automated E2E test project")

        # Submit
        page.click("#submit-btn")

        # Wait for redirect to /project/{name}
        page.wait_for_url(f"**/project/{project_name}", timeout=120000)

        # Verify we're on the project page
        assert f"/project/{project_name}" in page.url

        # Wait for Ralphy's first message (appears as .chat-msg.assistant)
        assistant_msg = page.locator(".chat-msg.assistant")
        assistant_msg.first.wait_for(state="visible", timeout=30000)

    finally:
        delete_project_api(project_name)


def test_chat_flow(page: Page):
    """Create project, send a message, verify Ralphy responds."""
    project_name = f"e2e-chat-{int(time.time())}"

    try:
        # Create project via API to save time
        create_project_api(project_name)

        page.goto(f"{BASE_URL}/project/{project_name}")
        page.wait_for_load_state("domcontentloaded")

        # Wait for Ralphy's first message (auto-sent on new session)
        assistant_msg = page.locator(".chat-msg.assistant")
        assistant_msg.first.wait_for(state="visible", timeout=60000)

        # Wait for processing to finish (textarea becomes enabled)
        page.wait_for_function(
            "() => !document.getElementById('chat-input').disabled",
            timeout=60000,
        )

        # Type a message
        textarea = page.locator("#chat-input")
        textarea.fill("What is the first thing we should build?")

        # Tab + Enter to send
        textarea.press("Tab")
        textarea.press("Enter")

        # Wait for user message to appear
        user_msgs = page.locator(".chat-msg.user")
        user_msgs.last.wait_for(state="visible", timeout=5000)

        # Wait for Ralphy to respond (second assistant message)
        page.wait_for_function(
            "() => document.querySelectorAll('.chat-msg.assistant').length >= 2",
            timeout=60000,
        )

        # Verify the response is not an error
        last_assistant = assistant_msg.last
        text = last_assistant.inner_text()
        assert "Failed to send message" not in text
        assert len(text) > 10  # sanity check: got a real response

    finally:
        delete_project_api(project_name)


def test_chat_persistence(page: Page):
    """Send a message, reload the page, verify messages persist from localStorage."""
    project_name = f"e2e-persist-{int(time.time())}"

    try:
        create_project_api(project_name)

        page.goto(f"{BASE_URL}/project/{project_name}")
        page.wait_for_load_state("domcontentloaded")

        # Wait for Ralphy's first message
        assistant_msg = page.locator(".chat-msg.assistant")
        assistant_msg.first.wait_for(state="visible", timeout=60000)

        # Wait for processing to finish
        page.wait_for_function(
            "() => !document.getElementById('chat-input').disabled",
            timeout=60000,
        )

        # Reload the page
        page.reload()
        page.wait_for_load_state("domcontentloaded")

        # Verify messages reappear from localStorage
        restored_msgs = page.locator(".chat-msg")
        restored_msgs.first.wait_for(state="visible", timeout=10000)

        count = restored_msgs.count()
        assert count >= 1, "No messages restored from localStorage after reload"

    finally:
        delete_project_api(project_name)


def test_upload_preview(page: Page):
    """Upload a text file and verify it can be previewed in the Uploads tab."""
    project_name = f"e2e-upload-{int(time.time())}"

    try:
        create_project_api(project_name)

        page.goto(f"{BASE_URL}/project/{project_name}")
        page.wait_for_load_state("domcontentloaded")

        page.locator(".chat-msg.assistant").first.wait_for(state="visible", timeout=60000)
        page.wait_for_function(
            "() => !document.getElementById('chat-input').disabled",
            timeout=60000,
        )

        page.click("button[data-tab='uploads']")
        page.locator("#upload-list").wait_for(state="visible", timeout=10000)

        page.locator("#upload-file-input").set_input_files({
            "name": "notes.md",
            "mimeType": "text/markdown",
            "buffer": b"# Preview test\n\nThis file should render in the preview pane.",
        })

        file_name = page.locator(".upload-item .file-name", has_text="notes.md")
        file_name.wait_for(state="visible", timeout=10000)
        file_name.click()

        preview_text = page.locator(".upload-inline-preview pre")
        preview_text.wait_for(state="visible", timeout=5000)
        assert "Preview test" in preview_text.inner_text()
        assert "render in the preview pane" in preview_text.inner_text()

    finally:
        delete_project_api(project_name)


def test_chat_attachment_can_be_cleared(page: Page):
    """Attach a file in chat, clear it, and verify it is removed."""
    project_name = f"e2e-chat-attach-clear-{int(time.time())}"

    try:
        create_project_api(project_name)

        page.goto(f"{BASE_URL}/project/{project_name}")
        page.wait_for_load_state("domcontentloaded")

        page.locator(".chat-msg.assistant").first.wait_for(state="visible", timeout=60000)
        page.wait_for_function(
            "() => !document.getElementById('chat-input').disabled",
            timeout=60000,
        )

        page.locator("#file-input").set_input_files({
            "name": "mock-image.png",
            "mimeType": "image/png",
            "buffer": b"\x89PNG\r\n\x1a\nmock",
        })

        attachment_names = page.locator("#attachment-names")
        attachment_names.wait_for(state="visible", timeout=5000)
        expect_text = attachment_names.inner_text()
        assert "mock-image.png" in expect_text

        attachment_names.locator("button").click()

        page.wait_for_function(
            """() => {
                const input = document.getElementById('file-input');
                const names = document.getElementById('attachment-names');
                return input.files.length === 0 && names.textContent.trim() === '';
            }""",
            timeout=5000,
        )

    finally:
        delete_project_api(project_name)


def test_project_delete(page: Page):
    """Create project, verify it shows on dashboard, delete it, verify it's gone."""
    project_name = f"e2e-del-{int(time.time())}"

    try:
        create_project_api(project_name)

        # Go to dashboard and verify the project appears
        page.goto(f"{BASE_URL}/dashboard")
        page.wait_for_load_state("domcontentloaded")

        # Wait for projects to load
        project_card = page.locator(f".project-card[data-name='{project_name}']")
        project_card.wait_for(state="visible", timeout=15000)

        # Click delete and accept confirm dialog
        page.on("dialog", lambda dialog: dialog.accept())
        delete_btn = project_card.locator(".btn-delete")
        delete_btn.click()

        # Wait for the card to disappear
        project_card.wait_for(state="hidden", timeout=15000)

        # Verify it's really gone by reloading
        page.reload()
        page.wait_for_load_state("domcontentloaded")

        # Wait for the project list to load (loading text goes away)
        page.wait_for_function(
            "() => { var el = document.getElementById('loading-text'); return !el || el.style.display === 'none'; }",
            timeout=10000,
        )

        # Verify the project card is not present
        count = page.locator(f".project-card[data-name='{project_name}']").count()
        assert count == 0, f"Project {project_name} still appears after deletion"

    except Exception:
        # Clean up if test failed before deletion
        delete_project_api(project_name)
        raise
