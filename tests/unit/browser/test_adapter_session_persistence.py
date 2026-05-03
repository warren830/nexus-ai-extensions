"""Unit tests for BrowserBridgeAdapter's ``browser_sessions`` write path.

Covers the post-migration (DDB → PG) behavior introduced alongside the
three-DB architecture alignment (see
``api/v2/database/__init__.py`` three-DB notes):

    * First navigate/act/observe creates a row via ``create_browser_session``.
    * Subsequent calls touch ``last_used_at`` (and active_url on navigate).
    * ``max_sessions_exceeded`` surfaces as a tool-level error without
      dispatching anything to the extension.
    * When ``user_id`` is not provided (legacy ctor call), writes become
      no-ops — no DB round trip at all.

The real ``PostgresClient.create_browser_session`` uses ON CONFLICT DO
UPDATE, so calling it twice for the same session is idempotent. We mock the
DB entirely here; integration behavior belongs in a pytest marker that hits
a live Aurora instance (deferred — see ``tests/integration`` convention).
"""
import asyncio
import os
import sys
import types as _types
import unittest
from unittest.mock import MagicMock, AsyncMock, patch


PROJECT_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# Stub optional C-extensions so ``api.v2.database`` importing works in a venv
# without psycopg. Same pattern as test_browser_service.py.
for _missing in (
    "psycopg",
    "psycopg.rows",
    "psycopg.types",
    "psycopg.types.json",
    "psycopg_pool",
    "valkey",
):
    sys.modules.setdefault(_missing, _types.ModuleType(_missing))

sys.modules["psycopg"].conninfo = _types.SimpleNamespace(
    make_conninfo=lambda **_: ""
)
sys.modules["psycopg.rows"].dict_row = MagicMock(name="dict_row")
sys.modules["psycopg.types.json"].Jsonb = MagicMock(name="Jsonb")
sys.modules["psycopg_pool"].ConnectionPool = MagicMock(name="ConnectionPool")


def _run(coro):
    # Use asyncio.run() — matches test_adapter.py's pattern and avoids the
    # "no current event loop" failure when pytest tears down prior loops.
    return asyncio.run(coro)


class TestAdapterWritePath(unittest.TestCase):
    """First-call write + subsequent touch."""

    def setUp(self):
        from nexus_utils.browser.adapter import BrowserBridgeAdapter

        self._db = MagicMock(name="PostgresClient")
        self.adapter = BrowserBridgeAdapter(
            session_id="sess-A",
            session_to_user_resolver=lambda s: None,
            user_id="user-A",
            db=self._db,
        )

    def test_navigate_calls_create_browser_session(self):
        with patch(
            "nexus_utils.browser.adapter.dispatch_browser_command",
            new=AsyncMock(return_value={"ok": True, "url": "https://example.com"}),
        ):
            result = _run(self.adapter.navigate(url="https://example.com"))

        self.assertTrue(result["ok"])
        self._db.create_browser_session.assert_called_once_with(
            "sess-A",
            "user-A",
            status="ACTIVE",
            active_url="https://example.com",
        )
        # Touch fires only after dispatch reports ok.
        self._db.touch_browser_session.assert_called_once_with(
            "sess-A", active_url="https://example.com"
        )

    def test_act_calls_create_then_touch(self):
        with patch(
            "nexus_utils.browser.adapter.dispatch_browser_command",
            new=AsyncMock(return_value={"ok": True, "action_taken": "click"}),
        ):
            result = _run(self.adapter.act(instruction="click submit"))

        self.assertTrue(result["ok"])
        self._db.create_browser_session.assert_called_once_with(
            "sess-A", "user-A", status="ACTIVE", active_url=None
        )
        self._db.touch_browser_session.assert_called_once_with(
            "sess-A", active_url=None
        )

    def test_observe_writes_but_does_not_pass_active_url(self):
        with patch(
            "nexus_utils.browser.adapter.dispatch_browser_command",
            new=AsyncMock(return_value={"ok": True, "ax_tree": []}),
        ):
            _run(self.adapter.observe(query="login form"))

        self._db.create_browser_session.assert_called_once()
        self.assertEqual(
            self._db.create_browser_session.call_args.kwargs.get("active_url"),
            None,
        )

    def test_touch_skipped_on_dispatch_failure(self):
        """If dispatch returns ok:False, we don't bump last_used_at.

        Rationale: the session is in a degraded state (extension offline,
        timeout, auth denied); bumping ``last_used_at`` would make the row
        look freshly active in the UI, which is misleading.
        """
        with patch(
            "nexus_utils.browser.adapter.dispatch_browser_command",
            new=AsyncMock(return_value={"ok": False, "error": "extension_offline"}),
        ):
            _run(self.adapter.navigate(url="https://example.com"))

        self._db.create_browser_session.assert_called_once()
        self._db.touch_browser_session.assert_not_called()

    def test_max_sessions_exceeded_short_circuits_dispatch(self):
        """ValueError('max_sessions_exceeded') bubbles out as a tool error.

        We must NOT dispatch to the extension when the user is over the cap —
        the whole point of the limit is to keep the extension from juggling
        too many tabs at once.
        """
        self._db.create_browser_session.side_effect = ValueError(
            "max_sessions_exceeded"
        )
        dispatch_mock = AsyncMock()
        with patch(
            "nexus_utils.browser.adapter.dispatch_browser_command",
            new=dispatch_mock,
        ):
            result = _run(self.adapter.navigate(url="https://example.com"))

        self.assertEqual(result, {"ok": False, "error": "max_sessions_exceeded"})
        dispatch_mock.assert_not_called()

    def test_non_limit_value_error_does_not_block_dispatch(self):
        """Other ValueErrors are logged and swallowed — dispatch still fires."""
        self._db.create_browser_session.side_effect = ValueError("schema_version")
        with patch(
            "nexus_utils.browser.adapter.dispatch_browser_command",
            new=AsyncMock(return_value={"ok": True, "url": "https://x"}),
        ):
            result = _run(self.adapter.navigate(url="https://x"))
        # Dispatch proceeded; wrapper path succeeded.
        self.assertTrue(result["ok"])

    def test_generic_exception_in_create_is_swallowed(self):
        """DB outage must not break the agent's tool call surface."""
        self._db.create_browser_session.side_effect = RuntimeError("pool closed")
        with patch(
            "nexus_utils.browser.adapter.dispatch_browser_command",
            new=AsyncMock(return_value={"ok": True, "url": "https://x"}),
        ):
            result = _run(self.adapter.navigate(url="https://x"))
        self.assertTrue(result["ok"])


class TestAdapterNoUserIdNoWrite(unittest.TestCase):
    """Legacy ctor path: user_id=None → writes are no-ops."""

    def test_no_user_id_means_no_db_calls(self):
        from nexus_utils.browser.adapter import BrowserBridgeAdapter

        db = MagicMock(name="PostgresClient")
        adapter = BrowserBridgeAdapter(
            session_id="sess-X",
            session_to_user_resolver=lambda s: "user-from-resolver",
            db=db,
            # user_id omitted — legacy behavior
        )

        with patch(
            "nexus_utils.browser.adapter.dispatch_browser_command",
            new=AsyncMock(return_value={"ok": True}),
        ):
            _run(adapter.navigate(url="https://x"))

        db.create_browser_session.assert_not_called()
        db.touch_browser_session.assert_not_called()


class TestResolverFallback(unittest.TestCase):
    """``_resolve_user_id`` bridges first-call chicken-and-egg.

    Sequence during the very first navigate:
      1. Agent calls browser_navigate.
      2. Adapter calls dispatch_browser_command(resolver=_resolve_user_id).
      3. dispatch calls store.resolve_extension_by_session(sid, _resolve_user_id).
      4. _resolve_user_id first asks session_to_user_resolver(sid) — returns
         None because the PG row doesn't exist yet.
      5. _resolve_user_id falls back to self.user_id (the value we knew at
         injection time). This is what lets the first dispatch actually reach
         the extension before the write is observable.
    """

    def test_resolver_uses_db_first(self):
        from nexus_utils.browser.adapter import BrowserBridgeAdapter

        adapter = BrowserBridgeAdapter(
            session_id="sess-P",
            session_to_user_resolver=lambda s: "db-user" if s == "sess-P" else None,
            user_id="ctor-user",
        )
        # DB path wins when it returns a value.
        self.assertEqual(adapter._resolve_user_id("sess-P"), "db-user")

    def test_resolver_falls_back_to_ctor_user_id(self):
        from nexus_utils.browser.adapter import BrowserBridgeAdapter

        adapter = BrowserBridgeAdapter(
            session_id="sess-P",
            session_to_user_resolver=lambda s: None,  # first-call: no row yet
            user_id="ctor-user",
        )
        self.assertEqual(adapter._resolve_user_id("sess-P"), "ctor-user")

    def test_resolver_returns_none_for_other_session_ids(self):
        from nexus_utils.browser.adapter import BrowserBridgeAdapter

        adapter = BrowserBridgeAdapter(
            session_id="sess-P",
            session_to_user_resolver=lambda s: None,
            user_id="ctor-user",
        )
        # Fallback is scoped to THIS adapter's session only — don't leak
        # identity across sessions.
        self.assertIsNone(adapter._resolve_user_id("sess-OTHER"))


if __name__ == "__main__":
    unittest.main()
