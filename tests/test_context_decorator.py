"""Tests for the with_context decorator.

The decorator is the *single* place context injection happens, so both
success envelopes (from ``build_ok``) and error envelopes (from
``build_error``) must come out with ``_context`` attached when the
bridge has fresh signals. Fetch failures must be swallowed — context
is best-effort, never allowed to shadow the tool's own result.
"""

from unittest.mock import patch

import pytest

from yade_mcp.bridge import context as ctx_module
from yade_mcp.bridge.context import with_context
from yade_mcp.contracts import build_error, build_ok


@pytest.fixture
def fake_context():
    return {
        "user_console": {
            "description": "test",
            "entries": [{"input": "O.iter"}],
        }
    }


@pytest.mark.asyncio
async def test_injects_on_success(fake_context):
    @with_context
    async def tool():
        return build_ok({"msg": "hi"})

    with patch.object(ctx_module, "fetch_bridge_context", return_value=fake_context):
        result = await tool()

    assert result["ok"] is True
    assert result["_context"] == fake_context


@pytest.mark.asyncio
async def test_injects_on_error(fake_context):
    """The whole point of the refactor: error branch gets context too."""

    @with_context
    async def tool():
        return build_error("boom", "failed")

    with patch.object(ctx_module, "fetch_bridge_context", return_value=fake_context):
        result = await tool()

    assert result["ok"] is False
    assert result["_context"] == fake_context


@pytest.mark.asyncio
async def test_omits_context_when_empty():
    """No fresh signals → no ``_context`` key at all (don't pollute with empty)."""

    @with_context
    async def tool():
        return build_ok({"msg": "hi"})

    with patch.object(ctx_module, "fetch_bridge_context", return_value=None):
        result = await tool()

    assert "_context" not in result


@pytest.mark.asyncio
async def test_swallows_fetch_failure():
    """Bridge fetch blowing up must not break the tool's own return."""

    @with_context
    async def tool():
        return build_ok({"msg": "hi"})

    async def boom():
        raise RuntimeError("bridge down")

    with patch.object(ctx_module, "fetch_bridge_context", side_effect=boom):
        result = await tool()

    assert result["ok"] is True
    assert result["data"]["msg"] == "hi"
    assert "_context" not in result


@pytest.mark.asyncio
async def test_preserves_tool_signature():
    """The decorator must pass through args, kwargs, and the return value."""

    @with_context
    async def tool(a, b, *, c):
        return build_ok({"sum": a + b + c})

    with patch.object(ctx_module, "fetch_bridge_context", return_value=None):
        result = await tool(1, 2, c=3)

    assert result["data"]["sum"] == 6


@pytest.mark.asyncio
async def test_prod_mode_strips_error_details():
    """YADE_MCP_DEBUG=0 must hide error.details from the LLM."""

    @with_context
    async def tool():
        return build_error(
            "boom",
            "failed",
            {"traceback": "<40 lines>", "exception_type": "NameError"},
        )

    with patch.object(ctx_module, "is_debug_mode", return_value=False), patch.object(
        ctx_module, "fetch_bridge_context", return_value=None
    ):
        result = await tool()

    assert result["ok"] is False
    assert result["error"]["code"] == "boom"
    assert result["error"]["message"] == "failed"
    # The whole details bag is gone in prod mode.
    assert "details" not in result["error"]


@pytest.mark.asyncio
async def test_debug_mode_keeps_error_details():
    """YADE_MCP_DEBUG=1 (default) must preserve error.details."""

    @with_context
    async def tool():
        return build_error("boom", "failed", {"traceback": "<40 lines>"})

    with patch.object(ctx_module, "is_debug_mode", return_value=True), patch.object(
        ctx_module, "fetch_bridge_context", return_value=None
    ):
        result = await tool()

    assert result["error"]["details"]["traceback"] == "<40 lines>"


@pytest.mark.asyncio
async def test_prod_mode_does_not_touch_ok_envelopes():
    """Prod-mode detail-strip only fires on error envelopes — ok
    envelopes can carry diagnostic data legitimately (captured output,
    warnings, etc.)."""

    @with_context
    async def tool():
        return build_ok({"details": "this is user data, not error metadata"})

    with patch.object(ctx_module, "is_debug_mode", return_value=False), patch.object(
        ctx_module, "fetch_bridge_context", return_value=None
    ):
        result = await tool()

    assert result["data"]["details"] == "this is user data, not error metadata"
