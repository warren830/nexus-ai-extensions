"""
Unit tests for nexus_utils.browser.tools (P1.2b).

Four agent-facing ``@tool`` functions backed by BrowserBridgeAdapter:
    * browser_navigate
    * browser_observe
    * browser_act
    * wait_for_human

A single factory ``create_browser_tools(session_id, resolver)`` returns all
four, mirroring ``nexus_utils.bridge.remote_shell_tool.create_remote_shell_tool``
— closures capture the per-session adapter so the Strands tools don't have to
re-resolve user identity on every call.

Contract covered here:
  * Factory returns exactly four callables in the documented order.
  * Each tool forwards to the corresponding BrowserBridgeAdapter method with
    matching kwargs (defaults preserved when args are omitted).
  * Adapter return values (both success dicts and error dicts) pass through
    untouched.
  * ``wait_for_human`` raises ``NotImplementedError`` when
    ``takeover_mode != "reply"`` — rule R6, no silent downgrade.
  * Two factory invocations produce independent adapters (no shared state).

All tests mock ``nexus_utils.browser.tools.BrowserBridgeAdapter`` via
``unittest.mock.patch`` so no real WebSocket / extension is required. The
tools themselves are ``async`` ``@tool`` functions (Strands natively supports
async), so tests invoke them with ``asyncio.run``.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _make_resolver(mapping=None):
    """In-memory session_to_user_resolver for tests."""
    mapping = mapping or {}

    def _resolve(session_id):
        return mapping.get(session_id)

    return _resolve


def _run(coro):
    """Drive a coroutine to completion in a fresh event loop."""
    return asyncio.run(coro)


def _build_patched_adapter_instance(
    navigate_result=None,
    observe_result=None,
    act_result=None,
    wait_for_human_result=None,
    wait_for_human_side_effect=None,
):
    """Create a MagicMock that looks like a BrowserBridgeAdapter instance.

    Each method is an AsyncMock returning the supplied dict (or empty dict if
    the test didn't care). ``wait_for_human`` can be configured with a
    ``side_effect`` (e.g. to raise ``NotImplementedError``) via
    ``wait_for_human_side_effect``.
    """
    instance = MagicMock()
    instance.navigate = AsyncMock(return_value=navigate_result or {})
    instance.observe = AsyncMock(return_value=observe_result or {})
    instance.act = AsyncMock(return_value=act_result or {})
    if wait_for_human_side_effect is not None:
        instance.wait_for_human = AsyncMock(side_effect=wait_for_human_side_effect)
    else:
        instance.wait_for_human = AsyncMock(
            return_value=wait_for_human_result or {}
        )
    return instance


# ─────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────

def test_create_browser_tools_returns_four_tools():
    """Factory returns a list of 4 callable Strands tools, in order."""
    from nexus_utils.browser.tools import create_browser_tools

    resolver = _make_resolver({"sess-x": "user-x"})
    tools = create_browser_tools(
        session_id="sess-x",
        session_to_user_resolver=resolver,
    )

    assert isinstance(tools, list)
    assert len(tools) == 4
    # All four must be callable — whether they're bare functions or Strands
    # DecoratedFunctionTool wrappers, Python "callable" is true for both.
    for t in tools:
        assert callable(t), f"tool {t!r} is not callable"

    # Enforce the documented order: navigate, observe, act, wait_for_human.
    # Tool names come from either ``__name__`` (plain fn) or ``tool_spec`` (Strands).
    names = []
    for t in tools:
        spec = getattr(t, "tool_spec", None)
        if isinstance(spec, dict) and "name" in spec:
            names.append(spec["name"])
        else:
            names.append(getattr(t, "__name__", ""))
    assert names == [
        "browser_navigate",
        "browser_observe",
        "browser_act",
        "wait_for_human",
    ]


# ─────────────────────────────────────────────
# browser_navigate
# ─────────────────────────────────────────────

def test_browser_navigate_calls_adapter():
    """browser_navigate(url=...) -> adapter.navigate(url=...), return passthrough."""
    from nexus_utils.browser import tools as tools_mod

    adapter_instance = _build_patched_adapter_instance(
        navigate_result={"ok": True, "url": "https://g.com", "title": "GitHub"},
    )
    with patch.object(
        tools_mod, "BrowserBridgeAdapter", return_value=adapter_instance
    ) as adapter_cls:
        tool_list = tools_mod.create_browser_tools(
            session_id="sess-nav",
            session_to_user_resolver=_make_resolver(),
        )
        navigate_tool = tool_list[0]

        result = _run(navigate_tool(url="https://g.com"))

    # Adapter class constructed exactly once with session_id + resolver.
    assert adapter_cls.call_count == 1

    # Underlying adapter.navigate called with url kwarg.
    assert adapter_instance.navigate.await_count == 1
    call_kwargs = adapter_instance.navigate.await_args.kwargs
    call_args = adapter_instance.navigate.await_args.args

    url_value = call_kwargs.get("url", call_args[0] if call_args else None)
    assert url_value == "https://g.com"

    # Return passthrough.
    assert result == {"ok": True, "url": "https://g.com", "title": "GitHub"}


def test_browser_navigate_returns_error_on_offline():
    """If adapter returns {ok:False,error:...}, tool returns it verbatim."""
    from nexus_utils.browser import tools as tools_mod

    err = {"ok": False, "error": "extension_offline"}
    adapter_instance = _build_patched_adapter_instance(navigate_result=err)

    with patch.object(
        tools_mod, "BrowserBridgeAdapter", return_value=adapter_instance
    ):
        tool_list = tools_mod.create_browser_tools(
            session_id="sess-off",
            session_to_user_resolver=_make_resolver(),
        )
        navigate_tool = tool_list[0]

        result = _run(navigate_tool(url="https://nope"))

    assert result == err


# ─────────────────────────────────────────────
# browser_observe
# ─────────────────────────────────────────────

def test_browser_observe_default_args():
    """observe() no args -> adapter.observe(query=None, use_vision=False)."""
    from nexus_utils.browser import tools as tools_mod

    adapter_instance = _build_patched_adapter_instance(
        observe_result={"ok": True, "ax_tree": "<tree/>"},
    )
    with patch.object(
        tools_mod, "BrowserBridgeAdapter", return_value=adapter_instance
    ):
        tool_list = tools_mod.create_browser_tools(
            session_id="sess-obs",
            session_to_user_resolver=_make_resolver(),
        )
        observe_tool = tool_list[1]

        result = _run(observe_tool())

    assert adapter_instance.observe.await_count == 1
    kwargs = adapter_instance.observe.await_args.kwargs
    args = adapter_instance.observe.await_args.args

    query_value = kwargs.get("query", args[0] if args else None)
    use_vision_value = kwargs.get(
        "use_vision", args[1] if len(args) > 1 else None
    )

    assert query_value is None
    assert use_vision_value is False
    assert result == {"ok": True, "ax_tree": "<tree/>"}


def test_browser_observe_with_query_and_vision():
    """observe(query='form', use_vision=True) -> forwarded exactly."""
    from nexus_utils.browser import tools as tools_mod

    adapter_instance = _build_patched_adapter_instance(
        observe_result={"ok": True, "ax_tree": "<tree/>", "screenshot_b64": "xxx"},
    )
    with patch.object(
        tools_mod, "BrowserBridgeAdapter", return_value=adapter_instance
    ):
        tool_list = tools_mod.create_browser_tools(
            session_id="sess-obs2",
            session_to_user_resolver=_make_resolver(),
        )
        observe_tool = tool_list[1]

        _run(observe_tool(query="form", use_vision=True))

    kwargs = adapter_instance.observe.await_args.kwargs
    args = adapter_instance.observe.await_args.args

    query_value = kwargs.get("query", args[0] if args else None)
    use_vision_value = kwargs.get(
        "use_vision", args[1] if len(args) > 1 else None
    )
    assert query_value == "form"
    assert use_vision_value is True


# ─────────────────────────────────────────────
# browser_act
# ─────────────────────────────────────────────

def test_browser_act_sends_instruction():
    """browser_act(instruction=...) -> adapter.act called with instruction.

    Note: adapter.act also takes task_description and title_summary, but the
    tool signature only exposes ``instruction`` — the other two are filled in
    server-side (Wave A: empty strings) and documented as such in the docstring.
    """
    from nexus_utils.browser import tools as tools_mod

    adapter_instance = _build_patched_adapter_instance(
        act_result={"ok": True, "action_taken": "click", "target": "button#submit"},
    )
    with patch.object(
        tools_mod, "BrowserBridgeAdapter", return_value=adapter_instance
    ):
        tool_list = tools_mod.create_browser_tools(
            session_id="sess-act",
            session_to_user_resolver=_make_resolver(),
        )
        act_tool = tool_list[2]

        result = _run(act_tool(instruction="click the blue Submit button"))

    assert adapter_instance.act.await_count == 1
    kwargs = adapter_instance.act.await_args.kwargs
    args = adapter_instance.act.await_args.args

    instruction_value = kwargs.get("instruction", args[0] if args else None)
    assert instruction_value == "click the blue Submit button"

    assert result == {
        "ok": True,
        "action_taken": "click",
        "target": "button#submit",
    }


# ─────────────────────────────────────────────
# wait_for_human
# ─────────────────────────────────────────────

def test_wait_for_human_reply():
    """wait_for_human(reason=..., takeover_mode='reply') -> adapter passthrough."""
    from nexus_utils.browser import tools as tools_mod

    payload = {"ok": True, "user_response": "done", "note": "approved"}
    adapter_instance = _build_patched_adapter_instance(
        wait_for_human_result=payload,
    )
    with patch.object(
        tools_mod, "BrowserBridgeAdapter", return_value=adapter_instance
    ):
        tool_list = tools_mod.create_browser_tools(
            session_id="sess-h",
            session_to_user_resolver=_make_resolver(),
        )
        wait_tool = tool_list[3]

        result = _run(
            wait_tool(
                reason="need approval",
                suggestion="click OK",
                takeover_mode="reply",
                timeout=120,
            )
        )

    assert adapter_instance.wait_for_human.await_count == 1
    kwargs = adapter_instance.wait_for_human.await_args.kwargs
    args = adapter_instance.wait_for_human.await_args.args

    reason_value = kwargs.get("reason", args[0] if args else None)
    suggestion_value = kwargs.get(
        "suggestion", args[1] if len(args) > 1 else None
    )
    takeover_value = kwargs.get(
        "takeover_mode", args[2] if len(args) > 2 else None
    )
    timeout_value = kwargs.get("timeout", args[3] if len(args) > 3 else None)

    assert reason_value == "need approval"
    assert suggestion_value == "click OK"
    assert takeover_value == "reply"
    assert timeout_value == 120

    assert result == payload


def test_wait_for_human_interactive_raises_not_implemented():
    """takeover_mode='interactive' -> NotImplementedError (R6, no silent downgrade).

    The error may be raised by the adapter itself (adapter.wait_for_human is
    what enforces R6 per P1.2a) or pre-emptively by the tool — either way it
    must surface to the caller.
    """
    from nexus_utils.browser import tools as tools_mod

    # Mirror P1.2a adapter behavior: raise NotImplementedError when takeover
    # mode is unsupported.
    adapter_instance = _build_patched_adapter_instance(
        wait_for_human_side_effect=NotImplementedError(
            "takeover_mode='interactive' is reserved (R6)"
        ),
    )
    with patch.object(
        tools_mod, "BrowserBridgeAdapter", return_value=adapter_instance
    ):
        tool_list = tools_mod.create_browser_tools(
            session_id="sess-h2",
            session_to_user_resolver=_make_resolver(),
        )
        wait_tool = tool_list[3]

        with pytest.raises(NotImplementedError):
            _run(
                wait_tool(
                    reason="approve",
                    takeover_mode="interactive",
                )
            )


# ─────────────────────────────────────────────
# session_id threading
# ─────────────────────────────────────────────

def test_tools_preserve_session_id_across_calls():
    """All four tools from one factory share the same session_id (via closure)."""
    from nexus_utils.browser import tools as tools_mod

    adapter_instance = _build_patched_adapter_instance()
    with patch.object(
        tools_mod, "BrowserBridgeAdapter", return_value=adapter_instance
    ) as adapter_cls:
        tools_mod.create_browser_tools(
            session_id="sess-shared",
            session_to_user_resolver=_make_resolver({"sess-shared": "u-1"}),
        )

    # BrowserBridgeAdapter must have been instantiated with the session_id.
    assert adapter_cls.call_count == 1
    init_kwargs = adapter_cls.call_args.kwargs
    init_args = adapter_cls.call_args.args

    session_id_value = init_kwargs.get(
        "session_id", init_args[0] if init_args else None
    )
    assert session_id_value == "sess-shared"


def test_factory_creates_independent_adapters_per_session():
    """Two factory calls -> two BrowserBridgeAdapter instances, no shared state."""
    from nexus_utils.browser import tools as tools_mod

    with patch.object(tools_mod, "BrowserBridgeAdapter") as adapter_cls:
        # Each factory call yields a fresh mock adapter instance.
        adapter_cls.side_effect = [
            _build_patched_adapter_instance(),
            _build_patched_adapter_instance(),
        ]

        tools_mod.create_browser_tools(
            session_id="sess-a",
            session_to_user_resolver=_make_resolver({"sess-a": "user-a"}),
        )
        tools_mod.create_browser_tools(
            session_id="sess-b",
            session_to_user_resolver=_make_resolver({"sess-b": "user-b"}),
        )

    assert adapter_cls.call_count == 2

    # Compare session_ids across the two adapter constructions.
    call_a = adapter_cls.call_args_list[0]
    call_b = adapter_cls.call_args_list[1]

    sid_a = call_a.kwargs.get(
        "session_id", call_a.args[0] if call_a.args else None
    )
    sid_b = call_b.kwargs.get(
        "session_id", call_b.args[0] if call_b.args else None
    )

    assert sid_a == "sess-a"
    assert sid_b == "sess-b"
    assert sid_a != sid_b


# ─────────────────────────────────────────────
# Factory threading of resolver
# ─────────────────────────────────────────────

def test_factory_forwards_resolver_to_adapter():
    """The resolver callable is forwarded verbatim to BrowserBridgeAdapter."""
    from nexus_utils.browser import tools as tools_mod

    resolver = _make_resolver({"sess-r": "user-r"})
    with patch.object(tools_mod, "BrowserBridgeAdapter") as adapter_cls:
        adapter_cls.return_value = _build_patched_adapter_instance()

        tools_mod.create_browser_tools(
            session_id="sess-r",
            session_to_user_resolver=resolver,
        )

    assert adapter_cls.call_count == 1
    init_kwargs = adapter_cls.call_args.kwargs
    init_args = adapter_cls.call_args.args

    passed_resolver = init_kwargs.get(
        "session_to_user_resolver",
        init_args[1] if len(init_args) > 1 else None,
    )
    assert passed_resolver is resolver
