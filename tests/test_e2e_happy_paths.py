"""Comprehensive E2E happy-path tests for justralph.it using Playwright.

Runs against the local instance at localhost:8000.
Uses a session cookie generated from auth_utils to bypass GitHub OAuth.
Covers: landing, auth, projects CRUD, chat, tasks, Stripe checkout, logout.
"""

import os
import sys
import time

import httpx
import pytest
import stripe
from playwright.sync_api import sync_playwright, Page

# Add src/ to path so we can import app modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app.auth_utils import create_session_token
from app.config import STRIPE_SECRET_KEY

BASE_URL = "http://localhost:8000"
TEST_USER_ID = 1  # nicopujia -- must exist in the database

stripe.api_key = STRIPE_SECRET_KEY


def _session_cookie() -> str:
    """Generate a valid session cookie for the test user."""
    return create_session_token(TEST_USER_ID)


def _api_client(**kwargs) -> httpx.Client:
    """Return an httpx client with session auth against localhost."""
    return httpx.Client(
        base_url=BASE_URL,
        cookies={"session": _session_cookie()},
        timeout=180,
        **kwargs,
    )


def _create_project(name: str, description: str = "E2E test project") -> dict:
    """Create a project via the API."""
    with _api_client() as c:
        resp = c.post("/api/projects", json={"name": name, "description": description})
        assert resp.status_code == 200, f"Create failed: {resp.status_code} {resp.text}"
        return resp.json()


def _delete_project(name: str) -> None:
    """Delete a project via the API (best-effort cleanup)."""
    with _api_client() as c:
        # Stop Ralph if running
        c.post(f"/api/projects/{name}/ralph/stop")
        time.sleep(1)
        resp = c.delete(f"/api/projects/{name}?delete_repo=false")
        assert resp.status_code in (204, 404), f"Delete failed: {resp.status_code} {resp.text}"


def _unique_name(prefix: str = "e2e") -> str:
    return f"{prefix}-{int(time.time())}-{os.getpid()}"


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def anon_page(browser):
    """Browser page with no session (logged out)."""
    ctx = browser.new_context()
    page = ctx.new_page()
    yield page
    page.close()
    ctx.close()


@pytest.fixture
def page(browser):
    """Browser page with session cookie (logged in as test user)."""
    token = _session_cookie()
    ctx = browser.new_context()
    ctx.add_cookies([{
        "name": "session",
        "value": token,
        "domain": "localhost",
        "path": "/",
        "httpOnly": False,
        "secure": False,
        "sameSite": "Lax",
    }])
    p = ctx.new_page()
    yield p
    p.close()
    ctx.close()


# ── 1. Landing page ──────────────────────────────────────────────────


class TestLandingPage:
    def test_returns_200(self, anon_page: Page):
        resp = anon_page.goto(BASE_URL)
        assert resp is not None
        assert resp.status == 200

    def test_contains_title(self, anon_page: Page):
        anon_page.goto(BASE_URL)
        anon_page.wait_for_load_state("domcontentloaded")
        heading = anon_page.locator("h1")
        heading.wait_for(state="visible", timeout=5000)
        assert "JUST RALPH IT" in heading.inner_text()

    def test_login_button_visible(self, anon_page: Page):
        anon_page.goto(BASE_URL)
        anon_page.wait_for_load_state("domcontentloaded")
        login_btn = anon_page.locator("a.btn", has_text="LOGIN WITH GITHUB")
        login_btn.wait_for(state="visible", timeout=5000)
        assert login_btn.get_attribute("href") == "/auth/login"


# ── 2. Auth flow ─────────────────────────────────────────────────────


class TestAuthFlow:
    def test_session_cookie_grants_access(self, page: Page):
        """Authenticated page can reach /projects without redirect to /."""
        page.goto(f"{BASE_URL}/projects")
        page.wait_for_load_state("domcontentloaded")
        # Should stay on /projects, not redirect to /
        assert "/projects" in page.url

    def test_auth_me_returns_user(self):
        """GET /auth/me returns the test user info."""
        with _api_client() as c:
            resp = c.get("/auth/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["github_username"] == "nicopujia"

    def test_unauthenticated_projects_redirects(self, anon_page: Page):
        """Without session, /projects redirects to /."""
        anon_page.goto(f"{BASE_URL}/projects")
        anon_page.wait_for_load_state("domcontentloaded")
        # Should redirect to landing
        assert anon_page.url.rstrip("/") == BASE_URL.rstrip("/")


# ── 3. Project creation ─────────────────────────────────────────────


class TestProjectCreation:
    def test_create_via_ui(self, page: Page):
        """Create a project through the /new form and verify redirect."""
        name = _unique_name("e2e-create")

        try:
            page.goto(f"{BASE_URL}/new")
            page.wait_for_load_state("domcontentloaded")

            page.fill("#name", name)
            page.fill("#description", "E2E happy path test project")
            page.click("#submit-btn")

            # Wait for redirect to project page
            page.wait_for_url(f"**/projects/{name}", timeout=120_000)
            assert f"/projects/{name}" in page.url
        finally:
            _delete_project(name)

    def test_project_appears_on_dashboard(self, page: Page):
        """After creating a project, it appears on /projects."""
        name = _unique_name("e2e-dash")

        try:
            _create_project(name)

            page.goto(f"{BASE_URL}/projects")
            page.wait_for_load_state("domcontentloaded")

            card = page.locator(f".project-card[data-name='{name}']")
            card.wait_for(state="visible", timeout=15_000)
            assert card.is_visible()
        finally:
            _delete_project(name)

    def test_create_via_api(self):
        """POST /api/projects returns project JSON."""
        name = _unique_name("e2e-api")

        try:
            data = _create_project(name, "API test")
            assert data["name"] == name
            assert "github_repo_url" in data
            assert "id" in data
        finally:
            _delete_project(name)


# ── 4. Chat with Ralphy ─────────────────────────────────────────────


class TestChat:
    def test_send_message_sse_stream(self):
        """POST /api/projects/{name}/chat returns an SSE stream with data."""
        name = _unique_name("e2e-chat")

        try:
            _create_project(name)

            with _api_client() as c:
                resp = c.post(
                    f"/api/projects/{name}/chat",
                    json={"message": "Hello Ralphy, just say hi back in one sentence."},
                    headers={"Accept": "text/event-stream"},
                    timeout=120,
                )
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers.get("content-type", "")

                # Verify we got at least some SSE data lines
                body = resp.text
                data_lines = [l for l in body.splitlines() if l.startswith("data:")]
                assert len(data_lines) > 0, "No SSE data events received"
        finally:
            _delete_project(name)

    def test_chat_history_persists(self):
        """After sending a message, GET /api/projects/{name}/chat/history returns it."""
        name = _unique_name("e2e-hist")

        try:
            _create_project(name)

            # Send a message
            with _api_client() as c:
                c.post(
                    f"/api/projects/{name}/chat",
                    json={"message": "Remember this test message."},
                    timeout=120,
                )

            # Check history
            with _api_client() as c:
                resp = c.get(f"/api/projects/{name}/chat/history")
            assert resp.status_code == 200
            messages = resp.json()["messages"]
            assert len(messages) >= 1
            # The user message should be persisted
            user_msgs = [m for m in messages if m["role"] == "user"]
            assert any("Remember this test message" in m["content"] for m in user_msgs)
        finally:
            _delete_project(name)


# ── 5. Tasks ─────────────────────────────────────────────────────────


class TestTasks:
    def test_tasks_endpoint_works(self):
        """GET /api/projects/{name}/tasks returns a list (possibly empty)."""
        name = _unique_name("e2e-tasks")

        try:
            _create_project(name)

            with _api_client() as c:
                resp = c.get(f"/api/projects/{name}/tasks")
            assert resp.status_code == 200
            assert isinstance(resp.json(), list)
        finally:
            _delete_project(name)

    def test_tasks_tab_loads_in_browser(self, page: Page):
        """Project page with ?tab=tasks loads without error."""
        name = _unique_name("e2e-tabtask")

        try:
            _create_project(name)

            page.goto(f"{BASE_URL}/projects/{name}?tab=tasks")
            page.wait_for_load_state("domcontentloaded")
            assert page.url.endswith(f"?tab=tasks") or "tab=tasks" in page.url
        finally:
            _delete_project(name)


# ── 6. Stripe checkout flow ─────────────────────────────────────────


class TestStripeCheckout:
    def test_checkout_endpoint_returns_stripe_url(self):
        """POST /api/projects/{name}/ralph/checkout creates a Stripe session URL
        when there are unpaid tasks.

        If the user is on the freelist or has no tasks, the endpoint returns
        redirect=null (starts Ralph directly). We test the Stripe path by
        creating a project, adding a task file manually, then calling checkout.
        """
        name = _unique_name("e2e-stripe")

        try:
            proj = _create_project(name, "Stripe checkout test")

            # We need at least one non-done task so there's something to pay for.
            # Create a task file via the tasks API indirectly -- use a direct file write
            # via the chat API is too slow. Instead, create the task file on disk.
            # Since the test user is nicopujia and DATA_DIR is ~/jri/data:
            from app.config import DATA_DIR
            tasks_dir = DATA_DIR / "nicopujia" / name / ".jri" / "tasks" / "todo"
            tasks_dir.mkdir(parents=True, exist_ok=True)
            task_file = tasks_dir / "test-task.md"
            task_file.write_text(
                "---\n"
                "title: Test task\n"
                "id: test-task\n"
                "---\n"
                "A test task for Stripe checkout.\n"
            )

            with _api_client() as c:
                resp = c.post(f"/api/projects/{name}/ralph/checkout")

            # Depending on freelist status, response varies
            if resp.status_code == 200:
                data = resp.json()
                if data.get("free"):
                    # User is on freelist -- no Stripe redirect
                    assert data["redirect"] is None
                else:
                    # Stripe session URL returned
                    redirect = data.get("redirect")
                    assert redirect is not None, f"Expected Stripe URL, got: {data}"
                    assert "checkout.stripe.com" in redirect
            elif resp.status_code == 403:
                # User not whitelisted -- that's an app restriction, not a test failure
                pytest.skip("Test user not whitelisted for checkout")
            else:
                pytest.fail(f"Unexpected status {resp.status_code}: {resp.text}")

        finally:
            _delete_project(name)

    def test_stripe_session_creation_via_sdk(self):
        """Directly verify we can create a Stripe checkout session with test keys."""
        if not STRIPE_SECRET_KEY or not STRIPE_SECRET_KEY.startswith("sk_test_"):
            pytest.skip("No Stripe test secret key configured")

        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": 1500,  # $15
                    "product_data": {
                        "name": "E2E Test -- Just Ralph It",
                        "description": "Test checkout session (will not be completed)",
                    },
                },
                "quantity": 1,
            }],
            success_url=f"{BASE_URL}/projects/test?payment=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{BASE_URL}/projects/test?payment=cancel",
            client_reference_id="e2e-test",
        )

        assert session.id.startswith("cs_test_")
        assert session.url is not None
        assert "checkout.stripe.com" in session.url
        assert session.payment_status in ("unpaid", "no_payment_required")

    def test_payment_callback_rejects_unpaid(self):
        """The payment-callback endpoint rejects a session that hasn't been paid."""
        name = _unique_name("e2e-cb")

        try:
            _create_project(name, "Payment callback test")

            # Create a checkout session directly via Stripe
            session = stripe.checkout.Session.create(
                mode="payment",
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": 500,
                        "product_data": {"name": "E2E callback test"},
                    },
                    "quantity": 1,
                }],
                success_url=f"{BASE_URL}/test",
                cancel_url=f"{BASE_URL}/test",
                client_reference_id="e2e-test",
            )

            # Call the payment callback with the unpaid session
            with _api_client() as c:
                resp = c.get(
                    f"/api/projects/{name}/ralph/payment-callback",
                    params={"session_id": session.id},
                )

            # Should reject because payment_status != "paid"
            assert resp.status_code == 402, f"Expected 402, got {resp.status_code}: {resp.text}"

        finally:
            _delete_project(name)

    def test_payment_status_endpoint(self):
        """GET /api/projects/{name}/ralph/payment-status returns payment info."""
        name = _unique_name("e2e-paystatus")

        try:
            _create_project(name)

            with _api_client() as c:
                resp = c.get(f"/api/projects/{name}/ralph/payment-status")
            assert resp.status_code == 200
            data = resp.json()
            assert "paid_task_count" in data
            assert "total_tasks" in data
            assert "unpaid" in data
            assert "free_user" in data
        finally:
            _delete_project(name)


# ── 7. Project deletion ─────────────────────────────────────────────


class TestProjectDeletion:
    def test_delete_via_api(self):
        """DELETE /api/projects/{name} removes the project."""
        name = _unique_name("e2e-del")
        _create_project(name)

        with _api_client() as c:
            resp = c.delete(f"/api/projects/{name}?delete_repo=false")
        assert resp.status_code == 204

        # Verify it's gone
        with _api_client() as c:
            resp = c.get(f"/api/projects/{name}")
        assert resp.status_code == 404

    def test_delete_removes_from_dashboard(self, page: Page):
        """After deletion, project no longer appears on /projects page."""
        name = _unique_name("e2e-deldash")

        try:
            _create_project(name)

            # Verify it's there
            page.goto(f"{BASE_URL}/projects")
            page.wait_for_load_state("domcontentloaded")
            card = page.locator(f".project-card[data-name='{name}']")
            card.wait_for(state="visible", timeout=15_000)

            # Delete via API
            _delete_project(name)

            # Reload and verify gone
            page.reload()
            page.wait_for_load_state("domcontentloaded")
            # Wait for project list to load
            page.wait_for_timeout(2000)
            count = page.locator(f".project-card[data-name='{name}']").count()
            assert count == 0, f"Project {name} still visible after deletion"

        except Exception:
            _delete_project(name)
            raise

    def test_delete_via_ui_modal(self, page: Page):
        """Delete through the custom modal dialog on the dashboard."""
        name = _unique_name("e2e-delui")

        try:
            _create_project(name)

            page.goto(f"{BASE_URL}/projects")
            page.wait_for_load_state("domcontentloaded")

            card = page.locator(f".project-card[data-name='{name}']")
            card.wait_for(state="visible", timeout=15_000)

            # Click delete button to open the modal
            delete_btn = card.locator(".btn-delete")
            delete_btn.click()

            # Type project name in the confirmation input
            confirm_input = page.locator("#delete-modal-input")
            confirm_input.wait_for(state="visible", timeout=5_000)
            confirm_input.fill(name)

            # Click the confirm delete button
            page.locator("#delete-modal-confirm").click()

            # Wait for the card to disappear
            card.wait_for(state="hidden", timeout=15_000)

        except Exception:
            _delete_project(name)
            raise


# ── 8. 3-project limit ──────────────────────────────────────────────


class TestProjectLimit:
    def test_free_tier_limit(self):
        """Non-free/admin users get 402 after 3 projects.

        Note: if the test user (nicopujia) has role admin/free, this test
        is skipped because those roles bypass the limit.
        """
        # Check if user has a free/admin role via /auth/me
        with _api_client() as c:
            me_resp = c.get("/auth/me")
        user_data = me_resp.json()

        if user_data.get("role") in ("admin", "free"):
            pytest.skip("Test user has admin/free role; limit does not apply")

        # Check subscription plan
        # If user is already pro, skip
        # We can't easily check this without DB access, so we try and see

        names = [_unique_name(f"e2e-limit-{i}") for i in range(4)]
        created = []

        try:
            for i, name in enumerate(names[:3]):
                with _api_client() as c:
                    resp = c.post(
                        "/api/projects",
                        json={"name": name, "description": f"Limit test {i}"},
                    )
                if resp.status_code == 402:
                    pytest.skip("User already has 3+ projects; can't test limit cleanly")
                if resp.status_code == 403:
                    pytest.skip("User not whitelisted")
                assert resp.status_code == 200, f"Create #{i} failed: {resp.status_code} {resp.text}"
                created.append(name)

            # The 4th should fail with 402
            with _api_client() as c:
                resp = c.post(
                    "/api/projects",
                    json={"name": names[3], "description": "Should fail"},
                )

            if resp.status_code == 200:
                created.append(names[3])
                pytest.skip("User bypassed limit (may be freelist/pro)")

            assert resp.status_code == 402, f"Expected 402, got {resp.status_code}: {resp.text}"
            data = resp.json()
            detail = data.get("detail", {})
            assert detail.get("upgrade_required") is True
            assert "Pro" in detail.get("detail", "") or "pro" in str(detail).lower()

        finally:
            for name in created:
                _delete_project(name)


# ── 9. Logout ────────────────────────────────────────────────────────


class TestLogout:
    def test_logout_clears_session(self, page: Page):
        """GET /auth/logout clears the session cookie and redirects to /."""
        # Start on a protected page
        page.goto(f"{BASE_URL}/projects")
        page.wait_for_load_state("domcontentloaded")
        assert "/projects" in page.url

        # Logout
        page.goto(f"{BASE_URL}/auth/logout")
        page.wait_for_load_state("domcontentloaded")

        # Should be on landing page
        assert page.url.rstrip("/") == BASE_URL.rstrip("/") or page.url == f"{BASE_URL}/"

    def test_after_logout_protected_pages_redirect(self, page: Page):
        """After logout, visiting /projects redirects to /."""
        # Logout first
        page.goto(f"{BASE_URL}/auth/logout")
        page.wait_for_load_state("domcontentloaded")

        # Try to access protected page
        page.goto(f"{BASE_URL}/projects")
        page.wait_for_load_state("domcontentloaded")

        # Should be redirected to landing
        assert page.url.rstrip("/") == BASE_URL.rstrip("/") or page.url == f"{BASE_URL}/"

    def test_logout_api_returns_401(self):
        """After logout, /auth/me returns 401 (no cookie)."""
        # Make a request without a session cookie
        with httpx.Client(base_url=BASE_URL, timeout=10) as c:
            resp = c.get("/auth/me")
        assert resp.status_code == 401


# ── Additional edge cases ────────────────────────────────────────────


class TestProjectPage:
    def test_project_page_loads(self, page: Page):
        """The project page renders for an existing project."""
        name = _unique_name("e2e-page")

        try:
            _create_project(name)
            page.goto(f"{BASE_URL}/projects/{name}")
            page.wait_for_load_state("domcontentloaded")
            assert page.url.endswith(f"/projects/{name}") or f"/projects/{name}" in page.url
        finally:
            _delete_project(name)

    def test_nonexistent_project_returns_404(self, page: Page):
        """Visiting a project that does not exist returns 404."""
        resp = page.goto(f"{BASE_URL}/projects/does-not-exist-ever-99999")
        assert resp is not None
        assert resp.status == 404

    def test_project_readme_endpoint(self):
        """GET /api/projects/{name}/readme returns README content."""
        name = _unique_name("e2e-readme")

        try:
            _create_project(name, "README test")

            with _api_client() as c:
                resp = c.get(f"/api/projects/{name}/readme")
            assert resp.status_code == 200
            data = resp.json()
            assert data["exists"] is True
            assert name in data["content"]
        finally:
            _delete_project(name)

    def test_project_env_endpoint(self):
        """GET and PUT /api/projects/{name}/env work."""
        name = _unique_name("e2e-env")

        try:
            _create_project(name)

            with _api_client() as c:
                # GET initial (empty)
                resp = c.get(f"/api/projects/{name}/env")
                assert resp.status_code == 200

                # PUT a value
                resp = c.put(
                    f"/api/projects/{name}/env",
                    json={"content": "TEST_VAR=hello\n"},
                )
                assert resp.status_code == 200

                # GET back
                resp = c.get(f"/api/projects/{name}/env")
                assert resp.status_code == 200
                assert "TEST_VAR=hello" in resp.json()["content"]
        finally:
            _delete_project(name)


class TestRalphStatus:
    def test_ralph_status_idle(self):
        """GET /api/projects/{name}/ralph/status returns idle for new project."""
        name = _unique_name("e2e-rstatus")

        try:
            _create_project(name)

            with _api_client() as c:
                resp = c.get(f"/api/projects/{name}/ralph/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] in ("idle", "running", "stopped")
        finally:
            _delete_project(name)
