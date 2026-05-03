"""P1.3c — Unit tests for /browser/* FastAPI routes (api/v2/routers/browser.py).

The router is a thin HTTP boundary over ``browser_service`` (P1.3b). These
tests verify:

  1. Request/response shape and Pydantic validation.
  2. Proper translation of service error dicts to HTTP status codes
     (session_not_found -> 404, session_already_closed -> 409).
  3. **Ownership check** — a user must NOT be able to inspect or modify a
     session that belongs to another user (returns 403).
  4. Default and custom TTL flow for token issuance.

Test strategy:
  - Build a minimal FastAPI app with the router mounted at ``/browser``.
  - Override ``get_auth_user`` via ``app.dependency_overrides`` so tests can
    simulate any caller identity without real JWT.
  - Patch ``api.v2.routers.browser.browser_service`` (module-level import) to
    stub all service behaviour.
  - Use synchronous ``TestClient`` — FastAPI handles async/sync transparently.

Note on __init__.py:
  The sibling ``tests/unit/api/`` directory intentionally has NO __init__.py
  to avoid pytest shadowing the real ``api`` project package. We follow the
  same pattern here (no ``tests/unit/api/routers/__init__.py``).
"""
from __future__ import annotations

import os
import sys
import types as _types
import unittest
from unittest.mock import MagicMock, AsyncMock, patch


# Ensure project root is importable.
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# Stub optional C-extension modules that api.v2.database imports, so the
# router module imports cleanly in environments that don't ship psycopg/valkey.
def _fake_module(name: str):
    return _types.ModuleType(name)


for _missing in (
    "psycopg",
    "psycopg.rows",
    "psycopg.types",
    "psycopg.types.json",
    "psycopg_pool",
    "valkey",
):
    if _missing not in sys.modules:
        sys.modules[_missing] = _fake_module(_missing)

sys.modules["psycopg.rows"].dict_row = MagicMock(name="dict_row")
sys.modules["psycopg.types.json"].Jsonb = MagicMock(name="Jsonb")
sys.modules["psycopg_pool"].ConnectionPool = MagicMock(name="ConnectionPool")


from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_app(caller_user_id: str = "u1"):
    """Construct a fresh FastAPI app with the browser router mounted.

    Overrides ``get_auth_user`` so any endpoint that ``Depends`` on it gets
    a deterministic caller identity for the duration of the test.
    """
    from api.v2.routers import browser as browser_router_module
    from api.v2.auth.middleware import get_auth_user

    app = FastAPI()
    app.include_router(browser_router_module.router)
    app.dependency_overrides[get_auth_user] = lambda: {
        "user_id": caller_user_id,
        "username": caller_user_id,
        "role": "editor",
    }
    return app


class BrowserRoutesTestBase(unittest.TestCase):
    """Shared harness: build app + TestClient + mock browser_service."""

    caller = "u1"

    def setUp(self):
        # Patch the service BEFORE the client is built so each request
        # resolves ``browser_service`` symbols to the mock.
        self._svc_patcher = patch(
            "api.v2.routers.browser.browser_service"
        )
        self.mock_svc = self._svc_patcher.start()
        self.addCleanup(self._svc_patcher.stop)

        self.app = _build_app(caller_user_id=self.caller)
        self.client = TestClient(self.app)


# ─────────────────────────────────────────────
# POST /browser/token
# ─────────────────────────────────────────────

class TestIssueTokenRoute(BrowserRoutesTestBase):

    def test_issue_token_default_ttl(self):
        """POST /browser/token {} returns 200 with token/ttl/expires_at."""
        self.mock_svc.issue_token.return_value = {
            "token": "signed.token.value",
            "ttl": 28800,
            "expires_at": "2026-05-01T08:00:00Z",
        }

        resp = self.client.post("/browser/token", json={})

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["token"], "signed.token.value")
        self.assertEqual(body["ttl"], 28800)
        self.assertIn("T", body["expires_at"])
        # expiry_seconds NOT forwarded when body omits it
        call = self.mock_svc.issue_token.call_args
        self.assertNotIn("expiry_seconds", call.kwargs)

    def test_issue_token_custom_expiry(self):
        """Body expiry_seconds=3600 is forwarded to service."""
        self.mock_svc.issue_token.return_value = {
            "token": "t",
            "ttl": 3600,
            "expires_at": "2026-05-01T01:00:00Z",
        }

        resp = self.client.post("/browser/token", json={"expiry_seconds": 3600})

        self.assertEqual(resp.status_code, 200, resp.text)
        call = self.mock_svc.issue_token.call_args
        self.assertEqual(call.kwargs.get("expiry_seconds"), 3600)

    def test_issue_token_expiry_too_large_rejected(self):
        """expiry_seconds > 7 days (604800) triggers pydantic 422."""
        resp = self.client.post(
            "/browser/token", json={"expiry_seconds": 1_000_000}
        )
        self.assertEqual(resp.status_code, 422)

    def test_issue_token_expiry_too_small_rejected(self):
        """expiry_seconds < 60 triggers pydantic 422."""
        resp = self.client.post("/browser/token", json={"expiry_seconds": 10})
        self.assertEqual(resp.status_code, 422)


# ─────────────────────────────────────────────
# POST /browser/token/revoke
# ─────────────────────────────────────────────

class TestRevokeRoute(BrowserRoutesTestBase):

    def test_revoke_all_happy_path(self):
        """Service returns sessions_closed=5 -> 200 + ok=True."""
        self.mock_svc.revoke_all = AsyncMock(return_value={"sessions_closed": 5})

        resp = self.client.post("/browser/token/revoke")

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["sessions_closed"], 5)
        self.mock_svc.revoke_all.assert_awaited_once_with(self.caller)


# ─────────────────────────────────────────────
# GET /browser/sessions
# ─────────────────────────────────────────────

class TestListSessionsRoute(BrowserRoutesTestBase):

    def _row(self, **over):
        base = {
            "session_id": "s1",
            "user_id": self.caller,
            "status": "ACTIVE",
            "active_url": "https://example.com",
            "created_at": "2026-05-01T00:00:00Z",
            "last_used_at": "2026-05-01T00:01:00Z",
        }
        base.update(over)
        return base

    def test_list_sessions_returns_list(self):
        """Service rows -> 200 + list of sessions."""
        rows = [self._row(session_id="s1"), self._row(session_id="s2")]
        self.mock_svc.list_user_sessions.return_value = rows

        resp = self.client.get("/browser/sessions")

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(len(body), 2)
        self.assertEqual(body[0]["session_id"], "s1")
        self.assertEqual(body[1]["session_id"], "s2")

    def test_list_sessions_with_status_filter(self):
        """?status=ACTIVE is forwarded to service."""
        self.mock_svc.list_user_sessions.return_value = []

        resp = self.client.get("/browser/sessions?status=ACTIVE")

        self.assertEqual(resp.status_code, 200)
        call = self.mock_svc.list_user_sessions.call_args
        # Accept either kw or positional
        status_arg = call.kwargs.get("status")
        if status_arg is None and len(call.args) >= 2:
            status_arg = call.args[1]
        self.assertEqual(status_arg, "ACTIVE")


# ─────────────────────────────────────────────
# GET /browser/sessions/{session_id}
# ─────────────────────────────────────────────

class TestGetSessionRoute(BrowserRoutesTestBase):

    def _status(self, user_id=None, **over):
        base = {
            "session_id": "s1",
            "user_id": user_id or self.caller,
            "status": "ACTIVE",
            "extension_online": True,
            "active_url": "https://example.com",
            "created_at": "2026-05-01T00:00:00Z",
            "last_used_at": "2026-05-01T00:01:00Z",
        }
        base.update(over)
        return base

    def test_get_session_happy_path(self):
        """Session belongs to caller -> 200 + full response."""
        self.mock_svc.get_session_status = AsyncMock(return_value=self._status())

        resp = self.client.get("/browser/sessions/s1")

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["session_id"], "s1")
        self.assertEqual(body["user_id"], self.caller)
        self.assertTrue(body["extension_online"])

    def test_get_session_not_found(self):
        """Service returns session_not_found -> 404."""
        self.mock_svc.get_session_status = AsyncMock(
            return_value={"ok": False, "error": "session_not_found"}
        )

        resp = self.client.get("/browser/sessions/missing")

        self.assertEqual(resp.status_code, 404)

    def test_get_session_forbidden_cross_user(self):
        """Session owned by another user -> 403."""
        self.mock_svc.get_session_status = AsyncMock(
            return_value=self._status(user_id="other_user")
        )

        resp = self.client.get("/browser/sessions/s1")

        self.assertEqual(resp.status_code, 403)


# ─────────────────────────────────────────────
# POST /browser/sessions/{session_id}/pause
# ─────────────────────────────────────────────

class TestPauseSessionRoute(BrowserRoutesTestBase):

    def _status(self, user_id=None, **over):
        base = {
            "session_id": "s1",
            "user_id": user_id or self.caller,
            "status": "ACTIVE",
            "extension_online": True,
            "active_url": None,
            "created_at": "2026-05-01T00:00:00Z",
            "last_used_at": "2026-05-01T00:00:00Z",
        }
        base.update(over)
        return base

    def test_pause_session_happy_path(self):
        """Owned session -> service.pause_session called, 200."""
        self.mock_svc.get_session_status = AsyncMock(return_value=self._status())
        self.mock_svc.pause_session = AsyncMock(
            return_value={
                "ok": True,
                "session_id": "s1",
                "status": "PAUSED",
                "dispatch_result": {"ok": True},
            }
        )

        resp = self.client.post("/browser/sessions/s1/pause")

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "PAUSED")
        self.mock_svc.pause_session.assert_awaited_once_with("s1")

    def test_pause_session_not_found(self):
        """Pre-flight status returns session_not_found -> 404; pause NOT called."""
        self.mock_svc.get_session_status = AsyncMock(
            return_value={"ok": False, "error": "session_not_found"}
        )
        self.mock_svc.pause_session = AsyncMock()

        resp = self.client.post("/browser/sessions/missing/pause")

        self.assertEqual(resp.status_code, 404)
        self.mock_svc.pause_session.assert_not_awaited()

    def test_pause_session_already_closed(self):
        """Service returns session_already_closed -> 409."""
        self.mock_svc.get_session_status = AsyncMock(return_value=self._status())
        self.mock_svc.pause_session = AsyncMock(
            return_value={"ok": False, "error": "session_already_closed"}
        )

        resp = self.client.post("/browser/sessions/s1/pause")

        self.assertEqual(resp.status_code, 409)

    def test_pause_session_forbidden_cross_user(self):
        """Ownership mismatch in pre-flight status -> 403; pause NOT called."""
        self.mock_svc.get_session_status = AsyncMock(
            return_value=self._status(user_id="other_user")
        )
        self.mock_svc.pause_session = AsyncMock()

        resp = self.client.post("/browser/sessions/s1/pause")

        self.assertEqual(resp.status_code, 403)
        self.mock_svc.pause_session.assert_not_awaited()


# ─────────────────────────────────────────────
# POST /browser/sessions/{session_id}/close
# ─────────────────────────────────────────────

class TestCloseSessionRoute(BrowserRoutesTestBase):

    def _status(self, user_id=None, **over):
        base = {
            "session_id": "s1",
            "user_id": user_id or self.caller,
            "status": "ACTIVE",
            "extension_online": False,
            "active_url": None,
            "created_at": "2026-05-01T00:00:00Z",
            "last_used_at": "2026-05-01T00:00:00Z",
        }
        base.update(over)
        return base

    def test_close_session_happy_path(self):
        """Owned session -> service.close_session called, 200."""
        self.mock_svc.get_session_status = AsyncMock(return_value=self._status())
        self.mock_svc.close_session = AsyncMock(
            return_value={"ok": True, "session_id": "s1", "status": "CLOSED"}
        )

        resp = self.client.post("/browser/sessions/s1/close")

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "CLOSED")
        self.mock_svc.close_session.assert_awaited_once_with("s1")

    def test_close_session_not_found(self):
        """Pre-flight status returns session_not_found -> 404; close NOT called."""
        self.mock_svc.get_session_status = AsyncMock(
            return_value={"ok": False, "error": "session_not_found"}
        )
        self.mock_svc.close_session = AsyncMock()

        resp = self.client.post("/browser/sessions/missing/close")

        self.assertEqual(resp.status_code, 404)
        self.mock_svc.close_session.assert_not_awaited()

    def test_close_session_forbidden_cross_user(self):
        """Ownership mismatch in pre-flight status -> 403; close NOT called."""
        self.mock_svc.get_session_status = AsyncMock(
            return_value=self._status(user_id="other_user")
        )
        self.mock_svc.close_session = AsyncMock()

        resp = self.client.post("/browser/sessions/s1/close")

        self.assertEqual(resp.status_code, 403)
        self.mock_svc.close_session.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
