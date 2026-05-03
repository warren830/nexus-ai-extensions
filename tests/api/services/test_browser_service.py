"""
P1.3b - Unit tests for BrowserService (api/v2/services/browser_service.py).

BrowserService is the business layer that bridges Wave B HTTP routes to:
  - token_service (stateless HMAC token issue + verify)
  - DynamoDBClient (browser_sessions persistence)
  - BrowserBridgeAdapter (runtime dispatch to extension)
  - connection_store (extension online check)

Test strategy:
  - Inject mock DynamoDBClient via constructor (DI)
  - Patch module-level ``token_service``, ``connection_store``, and
    ``BrowserBridgeAdapter`` so no real token / store / adapter is touched.
  - All tests are unit-level: no network, no AWS, no async runtime deps.

Note on __init__.py:
  The sibling ``tests/unit/api/`` directory deliberately has no __init__.py,
  so pytest does NOT shadow the real ``api`` project package. We follow the
  same pattern here (no ``tests/unit/api/services/__init__.py``).
"""
import os
import sys
import types as _types
import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch


# Add project root so ``api.v2.*`` resolves to the real project tree.
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# Stub out optional C-extension modules so importing api.v2.database doesn't
# explode in environments without psycopg / valkey installed.
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


# Helper: run an async coroutine to completion in a fresh loop.
def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class BrowserServiceTestBase(unittest.TestCase):
    """Shared setup: build a BrowserService with a mock DynamoDBClient."""

    def setUp(self):
        from api.v2.services.browser_service import BrowserService

        self._db_mock = MagicMock(name="DynamoDBClient")
        self.service = BrowserService(db=self._db_mock)


# ─────────────────────────────────────────────
# issue_token
# ─────────────────────────────────────────────

class TestIssueToken(BrowserServiceTestBase):

    def test_issue_token_returns_token_ttl_and_expires_at(self):
        """issue_token returns dict with token, ttl, expires_at."""
        with patch("api.v2.services.browser_service.token_service") as mock_ts:
            mock_ts.generate_browser_token.return_value = "signed.token.value"

            result = self.service.issue_token(user_id="u1")

        self.assertIn("token", result)
        self.assertEqual(result["token"], "signed.token.value")
        self.assertIn("ttl", result)
        self.assertIn("expires_at", result)
        # ISO 8601 string
        self.assertIsInstance(result["expires_at"], str)
        self.assertIn("T", result["expires_at"])

    def test_issue_token_default_ttl_is_8_hours(self):
        """Default expiry_seconds=28800 (8 hours) per DEFAULT_BROWSER_TOKEN_EXPIRY."""
        with patch("api.v2.services.browser_service.token_service") as mock_ts:
            mock_ts.generate_browser_token.return_value = "t"

            result = self.service.issue_token(user_id="u1")

        mock_ts.generate_browser_token.assert_called_once()
        # generate_browser_token(user_id, expiry_seconds=28800)
        call_kwargs = mock_ts.generate_browser_token.call_args
        # Accept either positional or kwargs
        ttl_arg = call_kwargs.kwargs.get("expiry_seconds")
        if ttl_arg is None and len(call_kwargs.args) >= 2:
            ttl_arg = call_kwargs.args[1]
        self.assertEqual(ttl_arg, 28800)
        self.assertEqual(result["ttl"], 28800)

    def test_issue_token_custom_ttl_forwards(self):
        """Passing expiry_seconds=3600 forwards to token_service."""
        with patch("api.v2.services.browser_service.token_service") as mock_ts:
            mock_ts.generate_browser_token.return_value = "t"

            result = self.service.issue_token(user_id="u1", expiry_seconds=3600)

        call_kwargs = mock_ts.generate_browser_token.call_args
        ttl_arg = call_kwargs.kwargs.get("expiry_seconds")
        if ttl_arg is None and len(call_kwargs.args) >= 2:
            ttl_arg = call_kwargs.args[1]
        self.assertEqual(ttl_arg, 3600)
        self.assertEqual(result["ttl"], 3600)

    def test_issue_token_passes_user_id(self):
        """user_id is forwarded to token_service.generate_browser_token."""
        with patch("api.v2.services.browser_service.token_service") as mock_ts:
            mock_ts.generate_browser_token.return_value = "t"

            self.service.issue_token(user_id="alice")

        call_args = mock_ts.generate_browser_token.call_args
        uid = call_args.kwargs.get("user_id")
        if uid is None and len(call_args.args) >= 1:
            uid = call_args.args[0]
        self.assertEqual(uid, "alice")


# ─────────────────────────────────────────────
# revoke_all
# ─────────────────────────────────────────────

class TestRevokeAll(BrowserServiceTestBase):

    def test_revoke_all_closes_sessions(self):
        """revoke_all returns {'sessions_closed': N} from DB bulk close."""
        self._db_mock.close_browser_sessions_by_user.return_value = 3

        result = _run(self.service.revoke_all(user_id="u1"))

        self.assertEqual(result, {"sessions_closed": 3})
        self._db_mock.close_browser_sessions_by_user.assert_called_once_with("u1")

    def test_revoke_all_zero_sessions(self):
        """revoke_all returns sessions_closed=0 when user has no sessions."""
        self._db_mock.close_browser_sessions_by_user.return_value = 0

        result = _run(self.service.revoke_all(user_id="u2"))

        self.assertEqual(result, {"sessions_closed": 0})


# ─────────────────────────────────────────────
# create_adapter
# ─────────────────────────────────────────────

class TestCreateAdapter(BrowserServiceTestBase):

    def test_create_adapter_uses_db_resolver(self):
        """create_adapter wires the adapter with db.get_browser_session_user_id."""
        adapter = self.service.create_adapter("s1")

        # The resolver is the bound method
        self.assertIs(
            adapter.session_to_user_resolver,
            self._db_mock.get_browser_session_user_id,
        )
        self.assertEqual(adapter.session_id, "s1")

    def test_create_adapter_uses_configured_timeout(self):
        """create_adapter's default_timeout_seconds falls back to 30 when config absent."""
        adapter = self.service.create_adapter("s1")

        # Default from config fallback is 30
        self.assertEqual(adapter.default_timeout_seconds, 30)


# ─────────────────────────────────────────────
# pause_session
# ─────────────────────────────────────────────

class TestPauseSession(BrowserServiceTestBase):

    def test_pause_session_happy_path(self):
        """ACTIVE session -> adapter.pause called, DDB updated to PAUSED."""
        self._db_mock.get_browser_session.return_value = {
            "session_id": "s1",
            "user_id": "u1",
            "status": "ACTIVE",
        }

        # Patch adapter class so its pause() returns an awaitable.
        with patch(
            "api.v2.services.browser_service.BrowserBridgeAdapter"
        ) as MockAdapterCls:
            mock_adapter_instance = MagicMock(name="adapter_instance")
            mock_adapter_instance.pause = AsyncMock(
                return_value={"ok": True, "paused": True}
            )
            MockAdapterCls.return_value = mock_adapter_instance

            result = _run(self.service.pause_session("s1"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], "s1")
        self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(result["dispatch_result"], {"ok": True, "paused": True})
        self._db_mock.update_browser_session_status.assert_called_once_with(
            "s1", "PAUSED"
        )
        mock_adapter_instance.pause.assert_awaited_once()

    def test_pause_session_not_found(self):
        """Unknown session -> error dict, adapter NOT called."""
        self._db_mock.get_browser_session.return_value = None

        with patch(
            "api.v2.services.browser_service.BrowserBridgeAdapter"
        ) as MockAdapterCls:
            result = _run(self.service.pause_session("unknown"))

        self.assertEqual(
            result, {"ok": False, "error": "session_not_found"}
        )
        MockAdapterCls.assert_not_called()
        self._db_mock.update_browser_session_status.assert_not_called()

    def test_pause_session_already_closed(self):
        """CLOSED session -> error dict, adapter NOT called."""
        self._db_mock.get_browser_session.return_value = {
            "session_id": "s2",
            "user_id": "u1",
            "status": "CLOSED",
        }

        with patch(
            "api.v2.services.browser_service.BrowserBridgeAdapter"
        ) as MockAdapterCls:
            result = _run(self.service.pause_session("s2"))

        self.assertEqual(
            result, {"ok": False, "error": "session_already_closed"}
        )
        MockAdapterCls.assert_not_called()
        self._db_mock.update_browser_session_status.assert_not_called()


# ─────────────────────────────────────────────
# close_session
# ─────────────────────────────────────────────

class TestCloseSession(BrowserServiceTestBase):

    def test_close_session_happy_path(self):
        """ACTIVE session -> DDB status=CLOSED, adapter NOT called."""
        self._db_mock.get_browser_session.return_value = {
            "session_id": "s1",
            "user_id": "u1",
            "status": "ACTIVE",
        }

        with patch(
            "api.v2.services.browser_service.BrowserBridgeAdapter"
        ) as MockAdapterCls:
            result = _run(self.service.close_session("s1"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], "s1")
        self.assertEqual(result["status"], "CLOSED")
        self._db_mock.update_browser_session_status.assert_called_once_with(
            "s1", "CLOSED"
        )
        # close does NOT dispatch to extension
        MockAdapterCls.assert_not_called()

    def test_close_session_not_found(self):
        """Unknown session -> error dict."""
        self._db_mock.get_browser_session.return_value = None

        result = _run(self.service.close_session("unknown"))

        self.assertEqual(
            result, {"ok": False, "error": "session_not_found"}
        )
        self._db_mock.update_browser_session_status.assert_not_called()


# ─────────────────────────────────────────────
# get_session_status
# ─────────────────────────────────────────────

class TestGetSessionStatus(BrowserServiceTestBase):

    def test_get_session_status_extension_online(self):
        """Online extension -> extension_online=True."""
        self._db_mock.get_browser_session.return_value = {
            "session_id": "s1",
            "user_id": "u1",
            "status": "ACTIVE",
            "active_url": "https://example.com",
            "last_used_at": "2026-05-01T00:00:00Z",
            "created_at": "2026-05-01T00:00:00Z",
        }

        with patch("api.v2.services.browser_service.connection_store") as mock_store:
            mock_store.get_extension_by_user.return_value = MagicMock(
                name="BridgeConnection"
            )

            result = _run(self.service.get_session_status("s1"))

        self.assertEqual(result["session_id"], "s1")
        self.assertEqual(result["user_id"], "u1")
        self.assertEqual(result["status"], "ACTIVE")
        self.assertTrue(result["extension_online"])
        self.assertEqual(result["active_url"], "https://example.com")
        mock_store.get_extension_by_user.assert_called_once_with("u1")

    def test_get_session_status_extension_offline(self):
        """No extension -> extension_online=False."""
        self._db_mock.get_browser_session.return_value = {
            "session_id": "s1",
            "user_id": "u1",
            "status": "ACTIVE",
            "active_url": None,
            "last_used_at": "2026-05-01T00:00:00Z",
            "created_at": "2026-05-01T00:00:00Z",
        }

        with patch("api.v2.services.browser_service.connection_store") as mock_store:
            mock_store.get_extension_by_user.return_value = None

            result = _run(self.service.get_session_status("s1"))

        self.assertFalse(result["extension_online"])
        self.assertIsNone(result["active_url"])

    def test_get_session_status_not_found(self):
        """Unknown session -> error dict."""
        self._db_mock.get_browser_session.return_value = None

        result = _run(self.service.get_session_status("unknown"))

        self.assertEqual(
            result, {"ok": False, "error": "session_not_found"}
        )


# ─────────────────────────────────────────────
# list_user_sessions
# ─────────────────────────────────────────────

class TestListUserSessions(BrowserServiceTestBase):

    def test_list_user_sessions_passes_through(self):
        """list_user_sessions wraps db.list_browser_sessions_by_user."""
        fake_rows = [
            {"session_id": "s1", "user_id": "u1", "status": "ACTIVE"},
            {"session_id": "s2", "user_id": "u1", "status": "PAUSED"},
        ]
        self._db_mock.list_browser_sessions_by_user.return_value = fake_rows

        result = self.service.list_user_sessions(user_id="u1")

        self.assertEqual(result, fake_rows)
        self._db_mock.list_browser_sessions_by_user.assert_called_once()

    def test_list_user_sessions_with_status_filter(self):
        """status=ACTIVE filter is forwarded to DB layer."""
        self._db_mock.list_browser_sessions_by_user.return_value = []

        self.service.list_user_sessions(user_id="u1", status="ACTIVE")

        call = self._db_mock.list_browser_sessions_by_user.call_args
        status_arg = call.kwargs.get("status")
        # Accept positional too
        if status_arg is None:
            # scan positional args after user_id
            if len(call.args) >= 2:
                status_arg = call.args[1]
        self.assertEqual(status_arg, "ACTIVE")


if __name__ == "__main__":
    unittest.main()
