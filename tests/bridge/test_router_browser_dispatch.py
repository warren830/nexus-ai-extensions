"""
Unit tests for Bridge Browser WebSocket endpoint (U0.3).

Covers:
- POST /bridge/browser/ws — WebSocket upgrade endpoint for Chrome Extension.
  * Token validation (browser token only; shell tokens rejected).
  * Handshake flow (extension.online -> extension.registered).
  * Protocol version semver check (MAJOR mismatch -> 4004; MINOR tolerance).
  * user_id consistency between token.uid and extension.online.user_id.
  * Message prefix dispatch (browser.*, human.*, user.*).
  * Heartbeat refresh of last_active_at.
  * Unknown prefix ignored without disconnect.
- dispatch_browser_command(session_id, msg, resolver, timeout) — server-side
  entry point for BridgeAdapter (Phase 1).
  * Resolves extension via session->user resolver.
  * Returns {"ok": False, "error": "extension_offline"} when no extension.
  * Returns {"ok": False, "error": "extension_timeout"} when no response in time.
  * Routes request_id -> pending Future round-trip.

These tests use the real token_service and real ConnectionStore (U0.1 / U0.2
already have independent unit tests). WebSocket tests use FastAPI TestClient.
"""
import asyncio
import json
import logging
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus_utils.bridge import router as bridge_router_module
from nexus_utils.bridge.connection_store import ConnectionStore, store as global_store
from nexus_utils.bridge.router import (
    CURRENT_PROTOCOL,
    dispatch_browser_command,
    router as bridge_router,
    _pending_browser_requests,
)
from nexus_utils.bridge.token_service import TokenService, token_service


# ─────────────────────────────────────────────
# Helpers & fixtures
# ─────────────────────────────────────────────

@pytest.fixture
def app() -> FastAPI:
    """Mount the bridge router under /bridge just like production."""
    a = FastAPI()
    a.include_router(bridge_router, prefix="/bridge")
    return a


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_store_and_pending():
    """Ensure each test starts with no leftover extensions or pending futures."""
    # Snapshot then clear the global singleton store (extension conns only).
    with global_store._lock:
        ext_ids = [
            cid for cid, c in global_store._by_id.items() if c.user_id
        ]
    for cid in ext_ids:
        global_store.disconnect(cid)
    # Clear any stale pending futures from previous tests.
    _pending_browser_requests.clear()
    yield
    # Post-test cleanup.
    with global_store._lock:
        ext_ids = [
            cid for cid, c in global_store._by_id.items() if c.user_id
        ]
    for cid in ext_ids:
        global_store.disconnect(cid)
    _pending_browser_requests.clear()


def _make_browser_token(user_id: str = "u1", expiry: int = 600) -> str:
    return token_service.generate_browser_token(user_id=user_id, expiry_seconds=expiry)


def _make_shell_token(
    session_id: str = "sess-1",
    agent_id: str = "agent-1",
    connection_id: str = "rc-1",
) -> str:
    return token_service.generate_token(
        session_id=session_id,
        agent_id=agent_id,
        connection_id=connection_id,
    )


# ─────────────────────────────────────────────
# Token validation tests
# ─────────────────────────────────────────────

def test_browser_ws_rejects_shell_token(client):
    """A shell token (type=connect) must not open a browser WS."""
    token = _make_shell_token()
    with pytest.raises(Exception) as exc_info:
        with client.websocket_connect(f"/bridge/browser/ws?token={token}"):
            pass
    # Starlette wraps WebSocket close as WebSocketDisconnect-like exception
    # whose .code attribute carries the status.
    # The server must have closed with 4001.
    err = exc_info.value
    code = getattr(err, "code", None)
    assert code == 4001, f"expected close code 4001, got {code!r} (err={err!r})"


def test_browser_ws_rejects_invalid_token(client):
    """Garbage token -> 4001."""
    with pytest.raises(Exception) as exc_info:
        with client.websocket_connect("/bridge/browser/ws?token=not-a-real-token"):
            pass
    assert getattr(exc_info.value, "code", None) == 4001


# ─────────────────────────────────────────────
# Handshake tests
# ─────────────────────────────────────────────

def test_browser_ws_accepts_browser_token_and_registers_extension(client):
    """Happy path: browser token -> extension.online -> extension.registered."""
    token = _make_browser_token(user_id="u-happy")
    with client.websocket_connect(f"/bridge/browser/ws?token={token}") as ws:
        ws.send_json({
            "type": "extension.online",
            "user_id": "u-happy",
            "version": "0.1.0",
            "protocol_version": CURRENT_PROTOCOL,
            "chrome_version": "124.0.0.0",
        })
        ack = ws.receive_json()
        assert ack["type"] == "extension.registered"
        assert "connection_id" in ack
        connection_id = ack["connection_id"]
        assert connection_id.startswith("ext-")

        # Store is updated server-side.
        conn = global_store.get_extension_by_user("u-happy")
        assert conn is not None
        assert conn.connection_id == connection_id
        assert conn.user_id == "u-happy"


def test_browser_ws_rejects_user_id_mismatch(client):
    """token.uid != extension.online.user_id -> close 4003."""
    token = _make_browser_token(user_id="u-token")
    with pytest.raises(Exception) as exc_info:
        with client.websocket_connect(f"/bridge/browser/ws?token={token}") as ws:
            ws.send_json({
                "type": "extension.online",
                "user_id": "u-different",
                "version": "0.1.0",
                "protocol_version": CURRENT_PROTOCOL,
                "chrome_version": "124.0.0.0",
            })
            # Server should close; attempt to read so exception surfaces.
            ws.receive_json()
    assert getattr(exc_info.value, "code", None) == 4003


# ─────────────────────────────────────────────
# Protocol version tests
# ─────────────────────────────────────────────

def test_browser_ws_rejects_major_version_mismatch(client):
    """Different MAJOR (server=1.x, client=2.0) -> close 4004."""
    token = _make_browser_token(user_id="u-major")
    with pytest.raises(Exception) as exc_info:
        with client.websocket_connect(f"/bridge/browser/ws?token={token}") as ws:
            ws.send_json({
                "type": "extension.online",
                "user_id": "u-major",
                "version": "0.1.0",
                "protocol_version": "2.0",
                "chrome_version": "124.0.0.0",
            })
            ws.receive_json()
    assert getattr(exc_info.value, "code", None) == 4004


def test_browser_ws_accepts_compatible_minor_version(client):
    """Same MAJOR, MINOR diff <= 1 -> accept."""
    token = _make_browser_token(user_id="u-minor")
    with client.websocket_connect(f"/bridge/browser/ws?token={token}") as ws:
        ws.send_json({
            "type": "extension.online",
            "user_id": "u-minor",
            "version": "0.1.0",
            "protocol_version": "1.1",
            "chrome_version": "124.0.0.0",
        })
        ack = ws.receive_json()
        assert ack["type"] == "extension.registered"


def test_browser_ws_warns_on_stale_minor_version(client, caplog):
    """Same MAJOR but stale MINOR -> accept + log warning."""
    caplog.set_level(logging.WARNING, logger="nexus_utils.bridge.router")
    token = _make_browser_token(user_id="u-stale")
    with client.websocket_connect(f"/bridge/browser/ws?token={token}") as ws:
        ws.send_json({
            "type": "extension.online",
            "user_id": "u-stale",
            # current is "1.0", so "0.5" is same-MAJOR-ish? No — MAJOR differs.
            # To test the stale-MINOR path we need CURRENT_PROTOCOL major == client major,
            # MINOR diff > 1. Use "1.5" if server is "1.0" (diff 5).
            "protocol_version": "1.5",
            "chrome_version": "124.0.0.0",
        })
        ack = ws.receive_json()
        assert ack["type"] == "extension.registered"
    # Look for "stale" in warning messages.
    warn_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("stale" in m.lower() or "minor" in m.lower() for m in warn_msgs), (
        f"expected a warning about stale minor version; got: {warn_msgs}"
    )


# ─────────────────────────────────────────────
# Message dispatch tests
# ─────────────────────────────────────────────

def test_browser_ws_heartbeat_marks_extension_active(client):
    """heartbeat -> heartbeat_ack + store refresh."""
    token = _make_browser_token(user_id="u-hb")
    with client.websocket_connect(f"/bridge/browser/ws?token={token}") as ws:
        ws.send_json({
            "type": "extension.online",
            "user_id": "u-hb",
            "version": "0.1.0",
            "protocol_version": CURRENT_PROTOCOL,
            "chrome_version": "124.0.0.0",
        })
        reg = ws.receive_json()
        conn = global_store.get_extension_by_user("u-hb")
        assert conn is not None
        old_active = conn.last_active_at

        time.sleep(0.02)

        ws.send_json({"type": "heartbeat"})
        ack = ws.receive_json()
        assert ack["type"] == "heartbeat_ack"

        # last_active_at should be refreshed.
        conn_after = global_store.get_extension_by_user("u-hb")
        assert conn_after is not None
        assert conn_after.last_active_at > old_active


def test_browser_ws_ignores_unknown_prefix_without_disconnect(client, caplog):
    """Unknown prefix -> warning log, connection stays open."""
    caplog.set_level(logging.WARNING, logger="nexus_utils.bridge.router")
    token = _make_browser_token(user_id="u-unk")
    with client.websocket_connect(f"/bridge/browser/ws?token={token}") as ws:
        ws.send_json({
            "type": "extension.online",
            "user_id": "u-unk",
            "version": "0.1.0",
            "protocol_version": CURRENT_PROTOCOL,
            "chrome_version": "124.0.0.0",
        })
        ws.receive_json()

        # Send a totally unknown-prefix message.
        ws.send_json({"type": "shell.foo", "data": "ignored"})

        # Connection must still be open: heartbeat round-trips.
        ws.send_json({"type": "heartbeat"})
        hb_ack = ws.receive_json()
        assert hb_ack["type"] == "heartbeat_ack"


# ─────────────────────────────────────────────
# dispatch_browser_command tests
# ─────────────────────────────────────────────

def test_dispatch_browser_command_returns_extension_offline():
    """No registered extension for user -> extension_offline."""
    def resolver(sid):
        return "u-offline"

    async def _run():
        return await dispatch_browser_command(
            session_id="sess-x",
            msg={"type": "browser.query_selector", "selector": "#foo"},
            session_to_user_resolver=resolver,
            timeout_seconds=1,
        )

    res = asyncio.run(_run())
    assert res == {"ok": False, "error": "extension_offline"}


def test_dispatch_browser_command_times_out():
    """Queue message but never resolve -> extension_timeout."""
    # Seed an extension for this user.
    global_store.register_extension(connection_id="ext-timeout", user_id="u-to")

    def resolver(sid):
        return "u-to"

    async def _run():
        return await dispatch_browser_command(
            session_id="sess-to",
            msg={"type": "browser.do_thing"},
            session_to_user_resolver=resolver,
            timeout_seconds=0.1,
        )

    res = asyncio.run(_run())
    assert res == {"ok": False, "error": "extension_timeout"}
    # Pending map should be cleaned up.
    assert _pending_browser_requests == {}


def test_dispatch_browser_command_routes_result_back_to_pending_future():
    """
    Start a dispatch that blocks on a Future, simulate the extension sending a
    browser.result with the same request_id, and verify the dispatch resolves
    with the data.
    """
    # Seed an extension for this user (simulates a live WS).
    conn = global_store.register_extension(connection_id="ext-rt", user_id="u-rt")

    def resolver(sid):
        return "u-rt"

    async def _scenario():
        dispatch_task = asyncio.create_task(
            dispatch_browser_command(
                session_id="sess-rt",
                msg={"type": "browser.query_selector", "selector": "#foo"},
                session_to_user_resolver=resolver,
                timeout_seconds=5,
            )
        )
        # Wait for dispatch to register pending future and enqueue msg.
        await asyncio.sleep(0.05)
        # Pull the queued message to read its request_id.
        queued = await asyncio.wait_for(conn._ws_queue.get(), timeout=1.0)
        assert "request_id" in queued, queued
        assert queued.get("session_id") == "sess-rt"
        rid = queued["request_id"]

        # Simulate the extension sending a browser.result with the same request_id.
        # Invoke the dispatch handler directly (no real WS loop).
        from nexus_utils.bridge.router import _handle_browser_message
        _handle_browser_message(
            msg={"type": "browser.result", "request_id": rid, "data": {"value": 42}},
            connection_id="ext-rt",
            user_id="u-rt",
        )

        result = await dispatch_task
        return result

    res = asyncio.run(_scenario())
    assert res == {"value": 42}


def test_handle_human_message_resolves_pending_future():
    """
    human.response with a request_id should resolve the pending future just
    like browser.result.
    """
    conn = global_store.register_extension(connection_id="ext-hum", user_id="u-hum")

    def resolver(sid):
        return "u-hum"

    async def _scenario():
        dispatch_task = asyncio.create_task(
            dispatch_browser_command(
                session_id="sess-hum",
                msg={"type": "human.ask", "question": "confirm?"},
                session_to_user_resolver=resolver,
                timeout_seconds=5,
            )
        )
        await asyncio.sleep(0.05)
        queued = await asyncio.wait_for(conn._ws_queue.get(), timeout=1.0)
        rid = queued["request_id"]

        from nexus_utils.bridge.router import _handle_human_message
        _handle_human_message(
            msg={"type": "human.response", "request_id": rid, "data": {"answer": "yes"}},
            connection_id="ext-hum",
            user_id="u-hum",
        )

        return await dispatch_task

    res = asyncio.run(_scenario())
    assert res == {"answer": "yes"}
