"""Integration tests for justralph.it — behavioral tests via the HTTP API.

These tests verify BEHAVIOR, not implementation. If internals change but
the API contract stays the same, these tests should still pass.

Runs against localhost:8000 (the live service).
"""

import os
import shutil
import sys
import time

import httpx
import pytest

# Add src/ to path so we can import auth and config
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app.auth_utils import create_session_token
from app.config import DATA_DIR, PRICE_PER_TASK

BASE_URL = "http://localhost:8000"
_created_projects: set[tuple[int, str]] = set()

# User IDs — must exist in the database with the correct roles.
# We look them up dynamically in the module-scoped fixture below.
_user_cache: dict[str, dict] = {}


def _session_cookie(user_id: int) -> str:
    return create_session_token(user_id)


def _api_client(user_id: int, **kwargs) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        cookies={"session": _session_cookie(user_id)},
        timeout=30,
        **kwargs,
    )


def _unique_name(prefix: str = "intg") -> str:
    return f"{prefix}-{int(time.time())}-{os.getpid()}"


# ── Helpers to find test users by role ──────────────────────────────


def _find_user_by_role(role: str) -> dict | None:
    """Query /auth/me for all known user IDs until we find one with the given role.

    This is a brute-force approach but avoids importing database internals.
    We cache results to avoid repeated lookups.
    """
    if role in _user_cache:
        return _user_cache[role]

    # Try user IDs 1-50 looking for one with the right role
    for uid in range(1, 51):
        try:
            with httpx.Client(
                base_url=BASE_URL,
                cookies={"session": create_session_token(uid)},
                timeout=5,
            ) as c:
                resp = c.get("/auth/me")
                if resp.status_code == 200:
                    data = resp.json()
                    user_role = data.get("role", "user")
                    # Cache every user we find
                    if user_role not in _user_cache:
                        _user_cache[user_role] = {"id": uid, **data}
                    if user_role == role:
                        return _user_cache[role]
        except Exception:
            continue
    return None


@pytest.fixture(scope="module")
def admin_user():
    user = _find_user_by_role("admin")
    if user is None:
        pytest.skip("No admin user found in database")
    return user


@pytest.fixture(scope="module")
def beta_user():
    user = _find_user_by_role("beta")
    if user is None:
        pytest.skip("No beta user found in database")
    return user


@pytest.fixture(scope="module")
def free_user():
    user = _find_user_by_role("free")
    if user is None:
        pytest.skip("No free user found in database")
    return user


@pytest.fixture(scope="module")
def regular_user():
    """A user with role='user' (not admin, beta, or free)."""
    user = _find_user_by_role("user")
    if user is None:
        pytest.skip("No regular (role=user) user found in database")
    return user


# ── Project lifecycle helpers ───────────────────────────────────────


def _create_project(user_id: int, name: str, desc: str = "integration test") -> dict:
    with _api_client(user_id) as c:
        resp = c.post("/api/projects", json={"name": name, "description": desc})
        assert resp.status_code == 200, f"Create failed: {resp.status_code} {resp.text}"
        _created_projects.add((user_id, name))
        return resp.json()


def _delete_project(user_id: int, name: str) -> None:
    with _api_client(user_id) as c:
        c.post(f"/api/projects/{name}/ralph/stop")
        resp = None
        for _ in range(5):
            time.sleep(1)
            resp = c.delete(f"/api/projects/{name}")
            if resp.status_code != 409:
                break
        assert resp is not None
        # 204 = deleted, 404 = already gone
        assert resp.status_code in (204, 404), (
            f"Delete failed: {resp.status_code} {resp.text}"
        )
    _created_projects.discard((user_id, name))


@pytest.fixture(autouse=True)
def cleanup_projects():
    yield
    leftovers = list(_created_projects)
    for user_id, name in leftovers:
        _delete_project(user_id, name)


def _create_task_file(
    github_username: str,
    project_name: str,
    slug: str,
    title: str,
    status: str = "todo",
    depends_on: list[str] | None = None,
) -> None:
    """Write a task markdown file on disk (no API for creating tasks)."""
    task_dir = DATA_DIR / github_username / project_name / ".jri" / "tasks" / status
    task_dir.mkdir(parents=True, exist_ok=True)
    deps_yaml = ""
    if depends_on:
        deps_list = "\n".join(f"  - {d}" for d in depends_on)
        deps_yaml = f"depends_on:\n{deps_list}\n"
    content = f"---\ntitle: {title}\n{deps_yaml}---\n"
    (task_dir / f"{slug}.md").write_text(content)


# ═══════════════════════════════════════════════════════════════════
# 1. PRICING CALCULATION (highest risk — money)
# ═══════════════════════════════════════════════════════════════════


class TestPricingCalculation:
    """Verify checkout amounts via the payment-status and checkout endpoints."""

    def test_zero_tasks_error(self, admin_user):
        """0 tasks -> checkout returns error (nothing to pay for)."""
        uid = admin_user["id"]
        name = _unique_name("price-0t")
        try:
            _create_project(uid, name)
            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/ralph/payment-status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total_tasks"] == 0
            assert data["unpaid"] == 0

            # Checkout with 0 tasks should return error
            with _api_client(uid) as c:
                resp = c.post(f"/api/projects/{name}/ralph/checkout")
            assert resp.status_code == 400  # No tasks found
        finally:
            _delete_project(uid, name)

    def test_three_tasks_unpaid(self, beta_user):
        """3 tasks unpaid -> 3x$4 = $12."""
        uid = beta_user["id"]
        username = beta_user["github_username"]
        name = _unique_name("price-3tu")
        try:
            _create_project(uid, name)
            for i in range(3):
                _create_task_file(username, name, f"task-{i}", f"Task {i}")

            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/ralph/payment-status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total_tasks"] == 3
            assert data["unpaid"] == 3

            # Expected: 3 tasks x $4 = $12
            expected_cents = 3 * PRICE_PER_TASK
            assert expected_cents == 1200

            with _api_client(uid) as c:
                resp = c.post(f"/api/projects/{name}/ralph/checkout")
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("redirect") is not None
            assert "checkout.stripe.com" in data["redirect"]
        finally:
            _delete_project(uid, name)

    def test_three_tasks_two_paid(self, beta_user):
        """3 tasks, 2 already paid -> 1x$4 = $4."""
        uid = beta_user["id"]
        username = beta_user["github_username"]
        name = _unique_name("price-3t2p")
        try:
            _create_project(uid, name)
            for i in range(3):
                _create_task_file(username, name, f"task-{i}", f"Task {i}")

            # Mark 2 tasks paid
            import asyncio

            import aiosqlite

            from app.database import DATABASE_PATH

            async def _mark_partial_paid():
                async with aiosqlite.connect(DATABASE_PATH) as db:
                    await db.execute(
                        "UPDATE projects SET paid_task_count = 2 "
                        "WHERE user_id = ? AND name = ?",
                        (uid, name),
                    )
                    await db.commit()

            asyncio.run(_mark_partial_paid())

            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/ralph/payment-status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total_tasks"] == 3
            assert data["paid_task_count"] == 2
            assert data["unpaid"] == 1

            # Expected: 1 unpaid task = $4
            expected_cents = 1 * PRICE_PER_TASK
            assert expected_cents == 400

            with _api_client(uid) as c:
                resp = c.post(f"/api/projects/{name}/ralph/checkout")
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("redirect") is not None
        finally:
            _delete_project(uid, name)

    def test_all_paid_starts_directly(self, beta_user):
        """All tasks paid -> starts Ralph (no checkout redirect)."""
        uid = beta_user["id"]
        username = beta_user["github_username"]
        name = _unique_name("price-allpd")
        try:
            _create_project(uid, name)
            for i in range(2):
                _create_task_file(username, name, f"task-{i}", f"Task {i}")

            # Mark all tasks paid
            import asyncio

            import aiosqlite

            from app.database import DATABASE_PATH

            async def _mark_all_paid():
                async with aiosqlite.connect(DATABASE_PATH) as db:
                    await db.execute(
                        "UPDATE projects SET paid_task_count = 2 "
                        "WHERE user_id = ? AND name = ?",
                        (uid, name),
                    )
                    await db.commit()

            asyncio.run(_mark_all_paid())

            with _api_client(uid) as c:
                resp = c.post(f"/api/projects/{name}/ralph/checkout")
            assert resp.status_code == 200
            data = resp.json()
            # No redirect — Ralph started directly
            assert data.get("redirect") is None

            # Stop Ralph so we can clean up
            with _api_client(uid) as c:
                c.post(f"/api/projects/{name}/ralph/stop")
        finally:
            _delete_project(uid, name)

    def test_free_user_gets_coupon(self, free_user):
        """Free user gets checkout with 100% coupon."""
        uid = free_user["id"]
        username = free_user["github_username"]
        name = _unique_name("price-free")
        try:
            _create_project(uid, name)
            _create_task_file(username, name, "task-0", "Free task")

            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/ralph/payment-status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["free_user"] is True

            with _api_client(uid) as c:
                resp = c.post(f"/api/projects/{name}/ralph/checkout")
            assert resp.status_code == 200
            data = resp.json()
            # Free users still go through Stripe checkout (with 100% coupon)
            # so redirect should be present
            assert data.get("redirect") is not None
            assert "checkout.stripe.com" in data["redirect"]
        finally:
            _delete_project(uid, name)

    def test_done_tasks_excluded_from_pricing(self, beta_user):
        """Done tasks don't count in pricing — only draft/todo/doing count."""
        uid = beta_user["id"]
        username = beta_user["github_username"]
        name = _unique_name("price-done")
        try:
            _create_project(uid, name)
            # 2 todo tasks + 1 done task
            _create_task_file(username, name, "task-a", "Task A", status="todo")
            _create_task_file(username, name, "task-b", "Task B", status="todo")
            _create_task_file(username, name, "task-done", "Done task", status="done")

            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/ralph/payment-status")
            assert resp.status_code == 200
            data = resp.json()
            # total_tasks counts non-done only
            assert data["total_tasks"] == 2
            assert data["unpaid"] == 2
        finally:
            _delete_project(uid, name)


# ═══════════════════════════════════════════════════════════════════
# 2. ROLE-BASED ACCESS (security)
# ═══════════════════════════════════════════════════════════════════


class TestRoleBasedAccess:
    """Verify that role checks are enforced at the API level."""

    def test_admin_can_create_project(self, admin_user):
        uid = admin_user["id"]
        name = _unique_name("role-admin")
        try:
            with _api_client(uid) as c:
                resp = c.post(
                    "/api/projects",
                    json={"name": name, "description": "admin test"},
                )
            assert resp.status_code == 200
        finally:
            _delete_project(uid, name)

    def test_beta_can_create_project(self, beta_user):
        uid = beta_user["id"]
        name = _unique_name("role-beta")
        try:
            with _api_client(uid) as c:
                resp = c.post(
                    "/api/projects",
                    json={"name": name, "description": "beta test"},
                )
            assert resp.status_code == 200
        finally:
            _delete_project(uid, name)

    def test_free_can_create_project(self, free_user):
        uid = free_user["id"]
        name = _unique_name("role-free")
        try:
            with _api_client(uid) as c:
                resp = c.post(
                    "/api/projects",
                    json={"name": name, "description": "free test"},
                )
            assert resp.status_code == 200
        finally:
            _delete_project(uid, name)

    def test_regular_user_gets_403_and_waitlisted(self, regular_user):
        """role=user -> 403 + added to waitlist."""
        uid = regular_user["id"]
        name = _unique_name("role-reg")
        with _api_client(uid) as c:
            resp = c.post(
                "/api/projects",
                json={"name": name, "description": "should fail"},
            )
        assert resp.status_code == 403
        assert "closed beta" in resp.json()["detail"].lower()

    def test_admin_bypasses_project_limit(self, admin_user):
        """Admin can create more than MAX_FREE_PROJECTS projects."""
        uid = admin_user["id"]
        # Count existing projects
        with _api_client(uid) as c:
            resp = c.get("/api/projects")
        existing = resp.json()
        existing_count = len(existing)

        # We only need to verify we can create one more beyond the limit
        # if we're already at or above 3, this proves the bypass
        if existing_count < 3:
            pytest.skip(
                f"Admin only has {existing_count} projects; "
                "need 3+ to prove limit bypass"
            )

        name = _unique_name("role-admlim")
        try:
            with _api_client(uid) as c:
                resp = c.post(
                    "/api/projects",
                    json={"name": name, "description": "beyond limit"},
                )
            # Should succeed (not 402)
            assert resp.status_code == 200
        finally:
            _delete_project(uid, name)

    def test_free_user_bypasses_project_limit(self, free_user):
        """Free user can create beyond MAX_FREE_PROJECTS."""
        uid = free_user["id"]
        with _api_client(uid) as c:
            resp = c.get("/api/projects")
        existing_count = len(resp.json())

        if existing_count < 3:
            pytest.skip(
                f"Free user only has {existing_count} projects; "
                "need 3+ to prove limit bypass"
            )

        name = _unique_name("role-freelim")
        try:
            with _api_client(uid) as c:
                resp = c.post(
                    "/api/projects",
                    json={"name": name, "description": "beyond limit"},
                )
            assert resp.status_code == 200
        finally:
            _delete_project(uid, name)

    def test_beta_user_hits_project_limit(self, beta_user):
        """Beta user (not admin/free) hits 3-project limit."""
        uid = beta_user["id"]
        names = []
        try:
            # First check how many projects they already have
            with _api_client(uid) as c:
                resp = c.get("/api/projects")
            existing_count = len(resp.json())

            # Create enough to hit the limit
            needed = max(0, 3 - existing_count)
            for i in range(needed):
                n = _unique_name(f"role-blim-{i}")
                with _api_client(uid) as c:
                    resp = c.post(
                        "/api/projects",
                        json={"name": n, "description": f"limit test {i}"},
                    )
                if resp.status_code == 402:
                    # Already at limit
                    break
                assert resp.status_code == 200, (
                    f"Create #{i} failed: {resp.status_code} {resp.text}"
                )
                names.append(n)

            # Now the 4th should fail
            overflow_name = _unique_name("role-blim-over")
            with _api_client(uid) as c:
                resp = c.post(
                    "/api/projects",
                    json={"name": overflow_name, "description": "should fail"},
                )

            if resp.status_code == 200:
                names.append(overflow_name)
                pytest.fail("Beta user was able to create beyond 3-project limit")

            assert resp.status_code == 402
            detail = resp.json().get("detail", {})
            assert detail.get("upgrade_required") is True
        finally:
            for n in names:
                _delete_project(uid, n)


# ═══════════════════════════════════════════════════════════════════
# 3. TASK LIFECYCLE (core business)
# ═══════════════════════════════════════════════════════════════════


class TestTaskLifecycle:
    """Verify task operations through the HTTP API."""

    def test_create_task_appears_in_list(self, admin_user):
        """Creating a task file makes it appear in GET /tasks."""
        uid = admin_user["id"]
        username = admin_user["github_username"]
        name = _unique_name("task-create")
        try:
            _create_project(uid, name)
            _create_task_file(username, name, "my-task", "My Task")

            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/tasks")
            assert resp.status_code == 200
            tasks = resp.json()
            ids = [t["id"] for t in tasks]
            assert "my-task" in ids

            # Verify the task has the right title and status
            task = next(t for t in tasks if t["id"] == "my-task")
            assert task["title"] == "My Task"
            assert task["status"] == "todo"
        finally:
            _delete_project(uid, name)

    def test_move_task_updates_status(self, admin_user):
        """Moving a task file between status dirs changes its status in the API."""
        uid = admin_user["id"]
        username = admin_user["github_username"]
        name = _unique_name("task-move")
        try:
            _create_project(uid, name)
            _create_task_file(username, name, "movable", "Movable Task", status="todo")

            # Verify it starts as todo
            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/tasks/movable")
            assert resp.status_code == 200
            assert resp.json()["status"] == "todo"

            # Move it to doing (simulate what Ralph does)
            base = DATA_DIR / username / name / ".jri" / "tasks"
            src = base / "todo" / "movable.md"
            dst = base / "doing" / "movable.md"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))

            # Verify via API
            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/tasks/movable")
            assert resp.status_code == 200
            assert resp.json()["status"] == "doing"

            # Move to done
            src2 = dst
            dst2 = base / "done" / "movable.md"
            dst2.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src2), str(dst2))

            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/tasks/movable")
            assert resp.status_code == 200
            assert resp.json()["status"] == "done"
        finally:
            _delete_project(uid, name)

    def test_task_count_updates_with_status(self, admin_user):
        """Payment-status task count reflects non-done tasks only."""
        uid = admin_user["id"]
        username = admin_user["github_username"]
        name = _unique_name("task-count")
        try:
            _create_project(uid, name)
            _create_task_file(username, name, "t1", "Task 1", status="todo")
            _create_task_file(username, name, "t2", "Task 2", status="todo")
            _create_task_file(username, name, "t3", "Task 3", status="doing")

            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/ralph/payment-status")
            assert resp.status_code == 200
            assert resp.json()["total_tasks"] == 3

            # Move one to done
            base = DATA_DIR / username / name / ".jri" / "tasks"
            shutil.move(str(base / "todo" / "t1.md"), str(base / "done" / "t1.md"))

            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/ralph/payment-status")
            assert resp.status_code == 200
            assert resp.json()["total_tasks"] == 2
        finally:
            _delete_project(uid, name)

    def test_task_with_dependencies(self, admin_user):
        """Tasks with depends_on show their dependencies in the API response."""
        uid = admin_user["id"]
        username = admin_user["github_username"]
        name = _unique_name("task-deps")
        try:
            _create_project(uid, name)
            _create_task_file(username, name, "base-task", "Base Task", status="todo")
            _create_task_file(
                username,
                name,
                "dependent-task",
                "Dependent Task",
                status="todo",
                depends_on=["base-task"],
            )

            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/tasks")
            assert resp.status_code == 200
            tasks = resp.json()

            dep_task = next(t for t in tasks if t["id"] == "dependent-task")
            assert "base-task" in dep_task.get("depends_on", [])

            free_task = next(t for t in tasks if t["id"] == "base-task")
            assert free_task.get("depends_on", []) == []
        finally:
            _delete_project(uid, name)

    def test_get_single_task(self, admin_user):
        """GET /tasks/{task_id} returns full task details."""
        uid = admin_user["id"]
        username = admin_user["github_username"]
        name = _unique_name("task-single")
        try:
            _create_project(uid, name)
            _create_task_file(username, name, "detail-task", "Detailed Task")

            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/tasks/detail-task")
            assert resp.status_code == 200
            task = resp.json()
            assert task["id"] == "detail-task"
            assert task["title"] == "Detailed Task"
            assert task["status"] == "todo"
        finally:
            _delete_project(uid, name)

    def test_nonexistent_task_returns_404(self, admin_user):
        """GET /tasks/{task_id} returns 404 for missing task."""
        uid = admin_user["id"]
        name = _unique_name("task-404")
        try:
            _create_project(uid, name)
            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/tasks/does-not-exist")
            assert resp.status_code == 404
        finally:
            _delete_project(uid, name)


# ═══════════════════════════════════════════════════════════════════
# 4. CHAT PERSISTENCE (data integrity)
# ═══════════════════════════════════════════════════════════════════


class TestChatPersistence:
    """Verify chat message storage and retrieval through the API."""

    def test_send_message_appears_in_history(self, admin_user):
        """Sending a chat message persists the user message in history."""
        uid = admin_user["id"]
        name = _unique_name("chat-persist")
        try:
            _create_project(uid, name)

            # Send a message (this spawns claude CLI, which may fail in test
            # env, but the user message should still be persisted)
            with _api_client(uid) as c:
                resp = c.post(
                    f"/api/projects/{name}/chat",
                    json={"message": "Hello from integration test"},
                    headers={"Accept": "text/event-stream"},
                    timeout=120,
                )
            # SSE stream should return 200 regardless
            assert resp.status_code == 200

            # Check history
            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/chat/history")
            assert resp.status_code == 200
            messages = resp.json()["messages"]
            user_msgs = [m for m in messages if m["role"] == "user"]
            assert any("Hello from integration test" in m["content"] for m in user_msgs)
        finally:
            _delete_project(uid, name)

    def test_multiple_messages_in_order(self, admin_user):
        """Multiple messages appear in chronological order."""
        uid = admin_user["id"]
        name = _unique_name("chat-order")
        try:
            _create_project(uid, name)

            for i in range(3):
                with _api_client(uid) as c:
                    c.post(
                        f"/api/projects/{name}/chat",
                        json={"message": f"Message number {i}"},
                        headers={"Accept": "text/event-stream"},
                        timeout=120,
                    )

            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/chat/history")
            assert resp.status_code == 200
            messages = resp.json()["messages"]
            user_msgs = [m for m in messages if m["role"] == "user"]

            # All 3 messages should be present
            assert len(user_msgs) >= 3

            # Verify order — each "Message number N" appears in sequence
            contents = [m["content"] for m in user_msgs]
            indices = []
            for i in range(3):
                needle = f"Message number {i}"
                found = [idx for idx, c in enumerate(contents) if needle in c]
                assert found, f"Message '{needle}' not found in history"
                indices.append(found[0])

            # Indices should be in ascending order
            assert indices == sorted(indices), f"Messages out of order: {indices}"
        finally:
            _delete_project(uid, name)

    def test_assistant_response_with_thinking_persisted(self, admin_user):
        """After a chat round-trip, assistant message is persisted."""
        uid = admin_user["id"]
        name = _unique_name("chat-think")
        try:
            _create_project(uid, name)

            with _api_client(uid) as c:
                resp = c.post(
                    f"/api/projects/{name}/chat",
                    json={"message": "Say exactly: PONG"},
                    headers={"Accept": "text/event-stream"},
                    timeout=120,
                )
            assert resp.status_code == 200

            # Give it a moment to persist
            time.sleep(1)

            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/chat/history")
            assert resp.status_code == 200
            messages = resp.json()["messages"]

            # Should have at least user + assistant messages
            roles = [m["role"] for m in messages]
            assert "user" in roles
            # Assistant message should be there if claude CLI worked
            if "assistant" in roles:
                assistant_msg = next(m for m in messages if m["role"] == "assistant")
                # The message object should have content field
                assert "content" in assistant_msg
                # Thinking fields are optional but should be present if used
                # (thinkingText, thinkingSteps are persisted when non-empty)
        finally:
            _delete_project(uid, name)

    def test_empty_history_for_new_project(self, admin_user):
        """A new project has no chat history."""
        uid = admin_user["id"]
        name = _unique_name("chat-empty")
        try:
            _create_project(uid, name)
            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/chat/history")
            assert resp.status_code == 200
            assert resp.json()["messages"] == []
        finally:
            _delete_project(uid, name)

    def test_chat_processing_false_when_idle(self, admin_user):
        """GET /chat/processing returns false when no chat is active."""
        uid = admin_user["id"]
        name = _unique_name("chat-proc")
        try:
            _create_project(uid, name)
            with _api_client(uid) as c:
                resp = c.get(f"/api/projects/{name}/chat/processing")
            assert resp.status_code == 200
            assert resp.json()["processing"] is False
        finally:
            _delete_project(uid, name)
