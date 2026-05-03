"""
Unit tests for nexus_utils.browser.adapter.BrowserBridgeAdapter (P1.2a).

The BrowserBridgeAdapter is a thin translation layer that turns agent-facing
browser_* tool calls into the browser.* / human.* / agent.* protocol messages
expected by the Chrome Extension, and dispatches them through the Phase 0
bridge router primitive ``dispatch_browser_command``.

Contract covered here:
  * Each adapter method builds the correct ``{"type": ..., "params": {...}}``
    message shape.
  * ``session_id`` and ``session_to_user_resolver`` are threaded through to
    ``dispatch_browser_command`` unchanged.
  * ``default_timeout_seconds`` is used for every non-human message.
  * For ``wait_for_human(timeout=N)`` the dispatch timeout is ``N + 10`` to
    leave the extension a buffer to reply after its own timer fires.
  * Success responses are normalized to ``{"ok": True, **data}``; error
    responses (``{"ok": False, "error": ...}``) are passed through untouched.
  * ``takeover_mode != "reply"`` raises ``NotImplementedError`` (R6: no silent
    downgrade for future-reserved modes).

All tests mock ``nexus_utils.browser.adapter.dispatch_browser_command`` with
``unittest.mock.AsyncMock`` so no real WebSocket / extension is required.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest


# ─────────────────────────────────────────────
# Test helpers
# ─────────────────────────────────────────────

def _make_resolver(mapping=None):
    """Build a minimal in-memory session_to_user_resolver.

    Tests only check that the resolver *object* is threaded through, so the
    actual mapping content rarely matters. Accepts a dict for convenience.
    """
    mapping = mapping or {}

    def _resolve(session_id):
        return mapping.get(session_id)

    return _resolve


def _run(coro):
    """Run an async coroutine inside a fresh event loop (pytest-asyncio-free)."""
    return asyncio.run(coro)


# ─────────────────────────────────────────────
# navigate
# ─────────────────────────────────────────────

def test_navigate_sends_correct_message_and_returns_wrapped_success():
    """navigate(url) -> correct msg shape + wrapped success payload."""
    from nexus_utils.browser.adapter import BrowserBridgeAdapter

    resolver = _make_resolver({"sess-1": "user-1"})
    adapter = BrowserBridgeAdapter(
        session_id="sess-1",
        session_to_user_resolver=resolver,
    )

    mock_dispatch = AsyncMock(return_value={"url": "https://x.y", "title": "T"})
    with patch(
        "nexus_utils.browser.adapter.dispatch_browser_command",
        new=mock_dispatch,
    ):
        result = _run(adapter.navigate(url="https://x.y"))

    # Verify dispatch was called exactly once with the right shape.
    assert mock_dispatch.await_count == 1
    kwargs = mock_dispatch.await_args.kwargs
    args = mock_dispatch.await_args.args

    # dispatch may be called positionally or by kwargs depending on impl
    # choice; inspect both.
    session_id = kwargs.get("session_id", args[0] if args else None)
    msg = kwargs.get("msg", args[1] if len(args) > 1 else None)
    passed_resolver = kwargs.get(
        "session_to_user_resolver", args[2] if len(args) > 2 else None
    )

    assert session_id == "sess-1"
    assert msg == {
        "type": "browser.navigate",
        "params": {"url": "https://x.y", "title_summary": ""},
    }
    # Adapter wraps the resolver in ``_resolve_user_id`` to bridge the first-
    # call chicken-and-egg (PG row doesn't exist yet). Verify the wrapper is
    # the method bound to this adapter and that it delegates to the original
    # resolver.
    assert passed_resolver == adapter._resolve_user_id
    assert passed_resolver("sess-1") == "user-1"  # delegates to resolver

    # Success wrapping: original data keys preserved, ok:True added.
    assert result == {"ok": True, "url": "https://x.y", "title": "T"}


def test_navigate_with_title_summary():
    """title_summary must land inside params."""
    from nexus_utils.browser.adapter import BrowserBridgeAdapter

    adapter = BrowserBridgeAdapter(
        session_id="sess-2",
        session_to_user_resolver=_make_resolver(),
    )

    mock_dispatch = AsyncMock(return_value={"url": "https://github.com/pr/1"})
    with patch(
        "nexus_utils.browser.adapter.dispatch_browser_command",
        new=mock_dispatch,
    ):
        _run(adapter.navigate(url="https://github.com/pr/1", title_summary="读 PR"))

    kwargs = mock_dispatch.await_args.kwargs
    args = mock_dispatch.await_args.args
    msg = kwargs.get("msg", args[1] if len(args) > 1 else None)

    assert msg["params"]["title_summary"] == "读 PR"
    assert msg["params"]["url"] == "https://github.com/pr/1"


def test_navigate_passes_through_extension_offline_error():
    """extension_offline error dict must be returned untouched (no wrapping)."""
    from nexus_utils.browser.adapter import BrowserBridgeAdapter

    adapter = BrowserBridgeAdapter(
        session_id="sess-3",
        session_to_user_resolver=_make_resolver(),
    )

    err = {"ok": False, "error": "extension_offline"}
    with patch(
        "nexus_utils.browser.adapter.dispatch_browser_command",
        new=AsyncMock(return_value=err),
    ):
        result = _run(adapter.navigate(url="https://a.b"))

    assert result == err


def test_navigate_passes_through_timeout():
    """extension_timeout error dict must be returned untouched."""
    from nexus_utils.browser.adapter import BrowserBridgeAdapter

    adapter = BrowserBridgeAdapter(
        session_id="sess-4",
        session_to_user_resolver=_make_resolver(),
    )

    err = {"ok": False, "error": "extension_timeout"}
    with patch(
        "nexus_utils.browser.adapter.dispatch_browser_command",
        new=AsyncMock(return_value=err),
    ):
        result = _run(adapter.navigate(url="https://a.b"))

    assert result == err


# ─────────────────────────────────────────────
# act
# ─────────────────────────────────────────────

def test_act_sends_all_three_fields():
    """act(instruction, task_description, title_summary) -> all in params."""
    from nexus_utils.browser.adapter import BrowserBridgeAdapter

    adapter = BrowserBridgeAdapter(
        session_id="sess-act",
        session_to_user_resolver=_make_resolver(),
    )

    mock_dispatch = AsyncMock(return_value={"success": True, "observation": "ok"})
    with patch(
        "nexus_utils.browser.adapter.dispatch_browser_command",
        new=mock_dispatch,
    ):
        result = _run(
            adapter.act(
                instruction="click submit",
                task_description="submit form",
                title_summary="提交表单",
            )
        )

    kwargs = mock_dispatch.await_args.kwargs
    args = mock_dispatch.await_args.args
    msg = kwargs.get("msg", args[1] if len(args) > 1 else None)

    assert msg["type"] == "browser.act"
    assert msg["params"]["instruction"] == "click submit"
    assert msg["params"]["task_description"] == "submit form"
    assert msg["params"]["title_summary"] == "提交表单"

    assert result == {"ok": True, "success": True, "observation": "ok"}


def test_act_authorization_denied_passthrough():
    """authorization_denied from extension should be returned as-is."""
    from nexus_utils.browser.adapter import BrowserBridgeAdapter

    adapter = BrowserBridgeAdapter(
        session_id="sess-act2",
        session_to_user_resolver=_make_resolver(),
    )

    err = {"ok": False, "error": "authorization_denied"}
    with patch(
        "nexus_utils.browser.adapter.dispatch_browser_command",
        new=AsyncMock(return_value=err),
    ):
        result = _run(adapter.act(instruction="do something"))

    assert result == err


# ─────────────────────────────────────────────
# observe
# ─────────────────────────────────────────────

def test_observe_default_no_vision():
    """observe() with no args -> use_vision=False by default."""
    from nexus_utils.browser.adapter import BrowserBridgeAdapter

    adapter = BrowserBridgeAdapter(
        session_id="sess-obs",
        session_to_user_resolver=_make_resolver(),
    )

    mock_dispatch = AsyncMock(return_value={"elements": []})
    with patch(
        "nexus_utils.browser.adapter.dispatch_browser_command",
        new=mock_dispatch,
    ):
        _run(adapter.observe())

    kwargs = mock_dispatch.await_args.kwargs
    args = mock_dispatch.await_args.args
    msg = kwargs.get("msg", args[1] if len(args) > 1 else None)

    assert msg["type"] == "browser.observe"
    assert msg["params"].get("use_vision") is False


def test_observe_with_query_and_vision():
    """observe(query=..., use_vision=True) -> params carry both."""
    from nexus_utils.browser.adapter import BrowserBridgeAdapter

    adapter = BrowserBridgeAdapter(
        session_id="sess-obs2",
        session_to_user_resolver=_make_resolver(),
    )

    mock_dispatch = AsyncMock(return_value={"elements": [{"tag": "form"}]})
    with patch(
        "nexus_utils.browser.adapter.dispatch_browser_command",
        new=mock_dispatch,
    ):
        result = _run(adapter.observe(query="login form", use_vision=True))

    kwargs = mock_dispatch.await_args.kwargs
    args = mock_dispatch.await_args.args
    msg = kwargs.get("msg", args[1] if len(args) > 1 else None)

    assert msg["type"] == "browser.observe"
    assert msg["params"]["query"] == "login form"
    assert msg["params"]["use_vision"] is True

    assert result["ok"] is True
    assert result["elements"] == [{"tag": "form"}]


# ─────────────────────────────────────────────
# wait_for_human
# ─────────────────────────────────────────────

def test_wait_for_human_reply_mode():
    """Default reply mode -> human.request with reason/suggestion/timeout."""
    from nexus_utils.browser.adapter import BrowserBridgeAdapter

    adapter = BrowserBridgeAdapter(
        session_id="sess-h",
        session_to_user_resolver=_make_resolver(),
    )

    mock_dispatch = AsyncMock(return_value={"reply": "go ahead"})
    with patch(
        "nexus_utils.browser.adapter.dispatch_browser_command",
        new=mock_dispatch,
    ):
        result = _run(
            adapter.wait_for_human(
                reason="need confirmation",
                suggestion="click approve",
                takeover_mode="reply",
                timeout=300,
            )
        )

    kwargs = mock_dispatch.await_args.kwargs
    args = mock_dispatch.await_args.args
    msg = kwargs.get("msg", args[1] if len(args) > 1 else None)

    assert msg["type"] == "human.request"
    assert msg["params"]["reason"] == "need confirmation"
    assert msg["params"]["suggestion"] == "click approve"
    assert msg["params"]["timeout"] == 300

    assert result == {"ok": True, "reply": "go ahead"}


def test_wait_for_human_interactive_returns_structured_error():
    """takeover_mode='interactive' -> structured tool error, no dispatch.

    R6 (no silent downgrade) is preserved but surfaced as a dict result
    instead of a raised exception — Strands would otherwise turn the
    raised exception into a raw agent-visible traceback, which is the
    wrong failure shape for a tool-call loop.
    """
    from nexus_utils.browser.adapter import BrowserBridgeAdapter

    adapter = BrowserBridgeAdapter(
        session_id="sess-h2",
        session_to_user_resolver=_make_resolver(),
    )

    mock_dispatch = AsyncMock(return_value={})
    with patch(
        "nexus_utils.browser.adapter.dispatch_browser_command",
        new=mock_dispatch,
    ):
        result = _run(
            adapter.wait_for_human(
                reason="approve action",
                takeover_mode="interactive",
            )
        )

    # Dispatch must NOT have been called (no silent downgrade).
    assert mock_dispatch.await_count == 0

    # Structured error shape — agent sees a normal tool failure.
    assert result["ok"] is False
    assert result["error"] == "unsupported_takeover_mode"
    assert result["requested_mode"] == "interactive"
    assert "reply" in result["supported_modes"]
    # R6: detail should mention "future" or "r6" for observability.
    detail = result.get("detail", "").lower()
    assert ("future" in detail) or ("r6" in detail)


def test_wait_for_human_uses_extended_dispatch_timeout():
    """user timeout=300 -> dispatch_browser_command timeout_seconds=310 (+10 buffer)."""
    from nexus_utils.browser.adapter import BrowserBridgeAdapter

    adapter = BrowserBridgeAdapter(
        session_id="sess-h3",
        session_to_user_resolver=_make_resolver(),
    )

    mock_dispatch = AsyncMock(return_value={"reply": "ok"})
    with patch(
        "nexus_utils.browser.adapter.dispatch_browser_command",
        new=mock_dispatch,
    ):
        _run(
            adapter.wait_for_human(
                reason="need confirmation",
                takeover_mode="reply",
                timeout=300,
            )
        )

    kwargs = mock_dispatch.await_args.kwargs
    args = mock_dispatch.await_args.args
    # dispatch_browser_command signature: (session_id, msg, resolver, timeout_seconds)
    dispatch_timeout = kwargs.get(
        "timeout_seconds", args[3] if len(args) > 3 else None
    )

    assert dispatch_timeout == 310  # 300 + 10 buffer


# ─────────────────────────────────────────────
# pause
# ─────────────────────────────────────────────

def test_pause_sends_agent_pause():
    """pause() -> agent.pause with empty params."""
    from nexus_utils.browser.adapter import BrowserBridgeAdapter

    adapter = BrowserBridgeAdapter(
        session_id="sess-p",
        session_to_user_resolver=_make_resolver(),
    )

    mock_dispatch = AsyncMock(return_value={"status": "paused"})
    with patch(
        "nexus_utils.browser.adapter.dispatch_browser_command",
        new=mock_dispatch,
    ):
        result = _run(adapter.pause())

    kwargs = mock_dispatch.await_args.kwargs
    args = mock_dispatch.await_args.args
    msg = kwargs.get("msg", args[1] if len(args) > 1 else None)

    # agent.pause is minimal per spec §4 example (no params key).
    assert msg == {"type": "agent.pause"}
    assert result == {"ok": True, "status": "paused"}


# ─────────────────────────────────────────────
# default timeout plumbing
# ─────────────────────────────────────────────

def test_adapter_uses_default_timeout_for_non_human_messages():
    """navigate / act / observe should dispatch with default_timeout_seconds."""
    from nexus_utils.browser.adapter import BrowserBridgeAdapter

    adapter = BrowserBridgeAdapter(
        session_id="sess-t",
        session_to_user_resolver=_make_resolver(),
        default_timeout_seconds=30,
    )

    mock_dispatch = AsyncMock(return_value={})
    with patch(
        "nexus_utils.browser.adapter.dispatch_browser_command",
        new=mock_dispatch,
    ):
        _run(adapter.navigate(url="https://a"))
        _run(adapter.act(instruction="i"))
        _run(adapter.observe())

    assert mock_dispatch.await_count == 3
    for call in mock_dispatch.await_args_list:
        args, kwargs = call.args, call.kwargs
        dispatch_timeout = kwargs.get(
            "timeout_seconds", args[3] if len(args) > 3 else None
        )
        assert dispatch_timeout == 30


def test_adapter_resolver_and_session_id_threaded_through():
    """Across multiple calls, session_id and resolver must be forwarded verbatim."""
    from nexus_utils.browser.adapter import BrowserBridgeAdapter

    resolver = _make_resolver({"sess-thread": "user-thread"})
    adapter = BrowserBridgeAdapter(
        session_id="sess-thread",
        session_to_user_resolver=resolver,
    )

    mock_dispatch = AsyncMock(return_value={})
    with patch(
        "nexus_utils.browser.adapter.dispatch_browser_command",
        new=mock_dispatch,
    ):
        _run(adapter.navigate(url="https://a"))
        _run(adapter.observe())
        _run(adapter.pause())

    assert mock_dispatch.await_count == 3
    for call in mock_dispatch.await_args_list:
        args, kwargs = call.args, call.kwargs
        session_id = kwargs.get("session_id", args[0] if args else None)
        passed_resolver = kwargs.get(
            "session_to_user_resolver", args[2] if len(args) > 2 else None
        )
        assert session_id == "sess-thread"
        # Adapter wraps the raw resolver in ``_resolve_user_id`` for the
        # first-call chicken-and-egg fix. Verify the wrapper points to the
        # same bound method and still delegates correctly.
        assert passed_resolver == adapter._resolve_user_id
