"""
Unit tests for ConnectionStore user-level extension connection index.

Covers U0.2 Interface Contracts:
- ConnectionStore.register_extension(connection_id, user_id, server_info=None) -> BridgeConnection
- ConnectionStore.get_extension_by_user(user_id) -> Optional[BridgeConnection]
- ConnectionStore.mark_extension_active(connection_id)
- ConnectionStore.resolve_extension_by_session(session_id, resolver) -> Optional[BridgeConnection]
- BridgeConnection now has a `user_id: str = ""` field (default empty preserves
  backwards-compat for shell connections).

These tests verify the Chrome Extension Phase 0 multi-device arbitration rule
(R5): when multiple extensions are registered for the same user, the most
recently active one wins.
"""
import threading
import time

import pytest

from nexus_utils.bridge.connection_store import BridgeConnection, ConnectionStore


@pytest.fixture
def store():
    """Fresh ConnectionStore for each test to avoid cross-test pollution."""
    return ConnectionStore()


class TestRegisterExtension:
    def test_register_extension_adds_to_user_index(self, store):
        """register_extension stores a connection indexed by user_id."""
        conn = store.register_extension(
            connection_id="ext-1",
            user_id="u1",
            server_info={},
        )

        assert isinstance(conn, BridgeConnection)
        assert conn.connection_id == "ext-1"
        assert conn.user_id == "u1"

        found = store.get_extension_by_user("u1")
        assert found is not None
        assert found.connection_id == "ext-1"
        assert found.user_id == "u1"

    def test_register_extension_server_info_defaults_to_empty_dict(self, store):
        """server_info=None should be normalised to an empty dict, like register()."""
        conn = store.register_extension(connection_id="ext-1", user_id="u1")
        assert conn.server_info == {}


class TestGetExtensionByUser:
    def test_get_extension_by_user_returns_none_when_absent(self, store):
        """Unknown user returns None (no KeyError)."""
        assert store.get_extension_by_user("nobody") is None

    def test_multiple_extensions_per_user_returns_most_recently_active(self, store):
        """
        R5 arbitration: when a user has two concurrent extension connections,
        get_extension_by_user returns the most recently active one.
        """
        store.register_extension(connection_id="ext-1", user_id="u1")
        # Ensure ext-1's last_active_at is clearly older than ext-2's registration time.
        time.sleep(0.01)
        store.register_extension(connection_id="ext-2", user_id="u1")

        # Bump ext-2 via mark_extension_active (simulates a fresh heartbeat).
        time.sleep(0.01)
        store.mark_extension_active("ext-2")

        winner = store.get_extension_by_user("u1")
        assert winner is not None
        assert winner.connection_id == "ext-2"

    def test_get_extension_by_user_after_newer_activity_on_first_conn(self, store):
        """Activity on the older conn should flip the winner back to it."""
        store.register_extension(connection_id="ext-1", user_id="u1")
        time.sleep(0.01)
        store.register_extension(connection_id="ext-2", user_id="u1")
        time.sleep(0.01)
        store.mark_extension_active("ext-1")

        winner = store.get_extension_by_user("u1")
        assert winner is not None
        assert winner.connection_id == "ext-1"


class TestResolveExtensionBySession:
    def test_resolve_extension_by_session_uses_mapping(self, store):
        """
        resolve_extension_by_session should delegate the session->user lookup
        to the injected resolver, then fall through to get_extension_by_user.
        """
        store.register_extension(connection_id="ext-1", user_id="u1")

        def resolver(session_id: str):
            return {"chat-1": "u1"}.get(session_id)

        found = store.resolve_extension_by_session("chat-1", resolver)
        assert found is not None
        assert found.connection_id == "ext-1"

    def test_resolve_extension_by_session_returns_none_if_resolver_returns_none(self, store):
        """Unknown session -> resolver returns None -> result is None."""
        store.register_extension(connection_id="ext-1", user_id="u1")

        def resolver(session_id: str):
            return None

        assert store.resolve_extension_by_session("chat-unknown", resolver) is None

    def test_resolve_extension_by_session_returns_none_if_user_has_no_extension(self, store):
        """Resolver gives a user_id that has no registered extension -> None."""

        def resolver(session_id: str):
            return "u-without-ext"

        assert store.resolve_extension_by_session("chat-x", resolver) is None


class TestDisconnectClearsUserIndex:
    def test_disconnect_extension_removes_from_user_index(self, store):
        """disconnect() must purge the user-level index entry as well."""
        store.register_extension(connection_id="ext-1", user_id="u1")
        assert store.get_extension_by_user("u1") is not None

        store.disconnect("ext-1")

        assert store.get_extension_by_user("u1") is None

    def test_disconnect_one_of_many_leaves_others(self, store):
        """If a user has two extensions, disconnecting one leaves the other findable."""
        store.register_extension(connection_id="ext-1", user_id="u1")
        store.register_extension(connection_id="ext-2", user_id="u1")

        store.disconnect("ext-1")

        remaining = store.get_extension_by_user("u1")
        assert remaining is not None
        assert remaining.connection_id == "ext-2"


class TestThreadSafety:
    def test_thread_safety_concurrent_register(self):
        """10 threads concurrently registering extensions for distinct users all succeed."""
        local_store = ConnectionStore()
        errors: list = []

        def worker(idx: int):
            try:
                local_store.register_extension(
                    connection_id=f"ext-{idx}",
                    user_id=f"u{idx}",
                )
            except Exception as e:  # pragma: no cover - surfaced via assertion below
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        for i in range(10):
            found = local_store.get_extension_by_user(f"u{i}")
            assert found is not None, f"u{i} lookup failed"
            assert found.connection_id == f"ext-{i}"

    def test_thread_safety_concurrent_register_same_user(self):
        """
        10 threads concurrently register distinct connections for the SAME user.
        All should land in the user index, and get_extension_by_user should
        return one of them (whichever was last active).
        """
        local_store = ConnectionStore()
        errors: list = []

        def worker(idx: int):
            try:
                local_store.register_extension(
                    connection_id=f"ext-{idx}",
                    user_id="shared-user",
                )
            except Exception as e:  # pragma: no cover - surfaced via assertion below
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        found = local_store.get_extension_by_user("shared-user")
        assert found is not None
        assert found.connection_id.startswith("ext-")


class TestBackwardsCompat:
    def test_existing_register_still_works_and_user_id_defaults_empty(self, store):
        """
        The existing register() path for shell connections must be untouched;
        BridgeConnection.user_id default must be "" so shell code doesn't break.
        """
        conn = store.register(
            connection_id="shell-1",
            session_id="sess-1",
            agent_id="agent-1",
        )
        assert conn.user_id == ""
        assert store.get("shell-1") is conn
        # Shell connections are NOT indexed by user.
        assert store.get_extension_by_user("") is None
