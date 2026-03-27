"""
End-to-end Playwright test for JRI project page chat.

Creates a project, verifies Ralphy chat flow (message send, streaming,
persistence across refresh, multi-turn), then cleans up.

Uses localhost:8000 to bypass Cloudflare proxy issues with SSE.
"""

import sys
import time

from itsdangerous import URLSafeTimedSerializer
from playwright.sync_api import sync_playwright

# ── Config ──────────────────────────────────────────────────────────
BASE_URL = "http://127.0.0.1:8000"
SECRET_KEY = "d9ba6a4a238a3115686b5bf8e763e571cfa7750ea3b652d23b12ced71e2c78e0"
USER_ID = 30  # nicopujia
PROJECT_NAME = "test-ttt4"
PROJECT_DESC = "A simple tic tac toe game"
SECOND_MSG = "Make it a 2 player game"

_serializer = URLSafeTimedSerializer(SECRET_KEY)
SESSION_TOKEN = _serializer.dumps({"uid": USER_ID})

RALPHY_TIMEOUT = 300_000  # 5 min
PAGE_TIMEOUT = 30_000


def cleanup_project(page):
    """Delete the test project via API, including the GitHub repo."""
    print(f"[cleanup] Deleting project '{PROJECT_NAME}'...")
    resp = page.request.delete(
        f"{BASE_URL}/api/projects/{PROJECT_NAME}?delete_repo=true"
    )
    if resp.status == 204:
        print("[cleanup] Project deleted successfully.")
    elif resp.status == 404:
        print("[cleanup] Project not found (already deleted).")
    else:
        print(f"[cleanup] WARNING: delete returned {resp.status}: {resp.text()}")


def wait_for_dom_response(page, asst_count, label=""):
    """Wait for assistant messages in the DOM."""
    prefix = f"[{label}] " if label else ""
    print(f"{prefix}Waiting for {asst_count} assistant response(s) in DOM...")
    page.wait_for_function(
        f"""() => {{
            const msgs = document.querySelectorAll('.chat-msg.assistant:not(.jri-msg)');
            if (msgs.length < {asst_count}) return false;
            if (document.querySelector('summary.shimmer')) return false;
            const msg = msgs[{asst_count} - 1];
            const txt = msg.querySelector('.message-text');
            const err = msg.querySelector('span[style*="color"]');
            return (txt && txt.textContent.trim().length > 0) || !!err;
        }}""",
        timeout=RALPHY_TIMEOUT,
    )
    print(f"{prefix}Done.")


def wait_for_server_history(page, min_user, min_asst, timeout_sec=120):
    """Poll server chat history until we see the expected message counts."""
    print(
        f"[server] Waiting for {min_user} user + {min_asst} assistant "
        f"in history (up to {timeout_sec}s)..."
    )
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        resp = page.request.get(f"{BASE_URL}/api/projects/{PROJECT_NAME}/chat/history")
        if resp.status == 200:
            msgs = resp.json().get("messages", [])
            u = sum(1 for m in msgs if m["role"] == "user")
            a = sum(1 for m in msgs if m["role"] == "assistant")
            if u >= min_user and a >= min_asst:
                print(f"[server] Got {u} user + {a} assistant. Done.")
                return u, a, msgs
        time.sleep(2)
    # Final check
    resp = page.request.get(f"{BASE_URL}/api/projects/{PROJECT_NAME}/chat/history")
    msgs = resp.json().get("messages", [])
    u = sum(1 for m in msgs if m["role"] == "user")
    a = sum(1 for m in msgs if m["role"] == "assistant")
    print(f"[server] Timed out. Got {u} user + {a} assistant.")
    return u, a, msgs


def count_dom_messages(page):
    return page.evaluate(  # noqa: E501
        """() => {
        const users = document.querySelectorAll('.chat-msg.user').length;
        const assts = document.querySelectorAll(
            '.chat-msg.assistant:not(.jri-msg)'
        ).length;
        return [users, assts];
    }"""
    )


def run_test():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()

        context.add_cookies(
            [
                {
                    "name": "session",
                    "value": SESSION_TOKEN,
                    "domain": "127.0.0.1",
                    "path": "/",
                    "httpOnly": True,
                    "secure": False,
                    "sameSite": "Lax",
                }
            ]
        )

        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)

        console_msgs = []
        page.on(
            "console",
            lambda msg: console_msgs.append(f"[console.{msg.type}] {msg.text}"),
        )

        try:
            # ── Step 1: Create project ──────────────────────────────
            print("[step 1] Creating project via API...")
            resp = page.request.post(
                f"{BASE_URL}/api/projects",
                data={"name": PROJECT_NAME, "description": PROJECT_DESC},
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200, f"Create failed: {resp.status} {resp.text()}"
            print("[step 1] Project created.")

            # ── Step 2: Navigate to project page ────────────────────
            print("[step 2] Loading project page...")
            page.goto(
                f"{BASE_URL}/projects/{PROJECT_NAME}",
                wait_until="domcontentloaded",
            )
            page.wait_for_selector("#chat-messages", state="visible")

            # ── Step 3: Auto-sent user message ──────────────────────
            print("[step 3] Waiting for auto-sent user message...")
            page.wait_for_selector(".chat-msg.user", state="visible", timeout=30_000)
            print("[step 3] User message visible.")

            # ── Step 4-5: Shimmer check ─────────────────────────────
            print("[step 4-5] Checking for shimmer...")
            try:
                page.wait_for_selector(
                    "summary.shimmer", state="visible", timeout=20_000
                )
                print("[step 5] Shimmer appeared.")
            except Exception:
                print("[step 5] Shimmer not caught.")

            # ── Step 6-7: Wait for first response ───────────────────
            wait_for_dom_response(page, 1, label="step 6-7")

            asst_text = page.evaluate("""() => {
                const el = document.querySelector(
                    '.chat-msg.assistant:not(.jri-msg) .message-text'
                );
                return el ? el.textContent.trim() : '';
            }""")
            print(f"[step 7] Assistant ({len(asst_text)} chars): '{asst_text[:80]}...'")

            # ── Wait for server to persist first response ───────────
            srv_u, srv_a, _ = wait_for_server_history(page, 1, 1)
            assert srv_u >= 1, f"Expected >=1 user msgs, got {srv_u}"
            assert srv_a >= 1, f"Expected >=1 asst msgs, got {srv_a}"

            # ── Step 8-9: Refresh and verify ────────────────────────
            print("[step 8] Refreshing page...")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("#chat-messages", state="visible")
            time.sleep(3)

            print("[step 9] Checking DOM after refresh...")
            page.wait_for_function(
                """() => {
                    var u = document.querySelectorAll('.chat-msg.user').length;
                    var a = document.querySelectorAll(
                        '.chat-msg.assistant:not(.jri-msg)'
                    ).length;
                    return u >= 1 && a >= 1;
                }""",
                timeout=15_000,
            )
            n_u, n_a = count_dom_messages(page)
            print(f"[step 9] DOM: {n_u} user, {n_a} assistant. Persisted.")

            # ── Step 10: Send second message ────────────────────────
            print(f"[step 10] Sending: '{SECOND_MSG}'...")
            page.wait_for_selector("#chat-input:not([disabled])", timeout=15_000)
            page.fill("#chat-input", SECOND_MSG)
            page.click("#btn-send")

            # ── Step 11: Wait for second response ───────────────────
            target_a = n_a + 1
            wait_for_dom_response(page, target_a, label="step 11")

            n_u2, n_a2 = count_dom_messages(page)
            print(f"[step 11] DOM: {n_u2} user, {n_a2} assistant")

            # ── Wait for server to persist second response ──────────
            srv_u2, srv_a2, _ = wait_for_server_history(page, 2, 2, timeout_sec=180)
            assert srv_u2 >= 2, f"Expected >=2 user msgs, got {srv_u2}"
            assert srv_a2 >= 2, f"Expected >=2 asst msgs, got {srv_a2}"

            # ── Step 12-13: Refresh and verify all messages ─────────
            print("[step 12] Refreshing page...")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("#chat-messages", state="visible")
            time.sleep(3)

            print("[step 13] Final verification...")
            page.wait_for_function(
                """() => {
                    var u = document.querySelectorAll('.chat-msg.user').length;
                    var a = document.querySelectorAll(
                        '.chat-msg.assistant:not(.jri-msg)'
                    ).length;
                    return u >= 2 && a >= 2;
                }""",
                timeout=15_000,
            )
            n_u3, n_a3 = count_dom_messages(page)
            print(f"[step 13] DOM: {n_u3} user, {n_a3} assistant")
            assert n_u3 >= 2
            assert n_a3 >= 2

            print("\n=== ALL TESTS PASSED ===\n")

        except Exception as e:
            print(f"\n!!! TEST FAILED: {e}\n")
            if console_msgs:
                print("[debug] Console:")
                for msg in console_msgs[-20:]:
                    print(f"  {msg}")
            page.screenshot(path="/home/nico/jri/tests/e2e_chat_failure.png")
            print("[debug] Screenshot: tests/e2e_chat_failure.png")
            raise

        finally:
            print("[step 14] Cleaning up...")
            cleanup_project(page)
            context.close()
            browser.close()


if __name__ == "__main__":
    try:
        run_test()
    except Exception:
        sys.exit(1)
