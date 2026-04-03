"""Tests for bridge client: connection lifecycle, error handling, retries."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yade_mcp.bridge.client import (
    BridgeResponseTooLarge,
    YADEBridgeClient,
    close_bridge_client,
    get_bridge_client,
)


def _make_client(**overrides):
    defaults = dict(
        url="ws://localhost:9002",
        reconnect_interval_s=0.1,
        max_retries=1,
        request_timeout_s=5.0,
        auto_reconnect=True,
    )
    defaults.update(overrides)
    return YADEBridgeClient(**defaults)


# =========================================================================
# Connection lifecycle
# =========================================================================

class TestConnectionLifecycle:
    @pytest.mark.asyncio
    async def test_connect_and_disconnect(self):
        client = _make_client()
        assert not client.connected

        await client.connect()
        assert client.connected

        await client.disconnect()
        assert not client.connected

    @pytest.mark.asyncio
    async def test_double_connect_is_noop(self):
        client = _make_client()
        await client.connect()
        ws1 = client._websocket
        await client.connect()  # should not reconnect
        assert client._websocket is ws1
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected(self):
        client = _make_client()
        await client.disconnect()  # should not raise
        assert not client.connected

    @pytest.mark.asyncio
    async def test_ensure_connected(self):
        client = _make_client()
        await client._ensure_connected()
        assert client.connected
        await client.disconnect()


# =========================================================================
# Request/response
# =========================================================================

class TestRequests:
    @pytest.mark.asyncio
    async def test_execute_code(self):
        client = _make_client()
        await client.connect()
        result = await client.execute_code("print('test')")
        assert result.get("status") in ("ok", "success")
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_check_task_status_not_found(self):
        client = _make_client()
        await client.connect()
        result = await client.check_task_status("nonexistent-id")
        assert result.get("status") == "not_found"
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_list_tasks(self):
        client = _make_client()
        await client.connect()
        result = await client.list_tasks(offset=0, limit=5)
        assert "data" in result or "status" in result
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_interrupt_nonexistent(self):
        client = _make_client()
        await client.connect()
        result = await client.interrupt_task("nonexistent-id")
        assert "status" in result
        await client.disconnect()


# =========================================================================
# Timeout and retry
# =========================================================================

class TestTimeoutAndRetry:
    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        client = _make_client(request_timeout_s=0.001, auto_reconnect=False, max_retries=0)
        await client.connect()

        # Patch send to not actually send (so no response comes back → timeout)
        client._websocket.send = AsyncMock()

        with pytest.raises(ConnectionError, match="failed"):
            await client.execute_code("print('slow')", timeout_ms=1)

    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        client = _make_client(max_retries=2, reconnect_interval_s=0.01, auto_reconnect=True)

        call_count = 0
        original_send_request = client._send_request

        async def flaky_send(message, timeout_s):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("simulated failure")
            return await original_send_request(message, timeout_s)

        client._send_request = flaky_send
        # max_retries=2 means 3 total attempts, the 3rd should succeed
        result = await client.execute_code("print('recovered')")
        assert call_count == 3
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_retry_exhausted(self):
        client = _make_client(max_retries=1, reconnect_interval_s=0.01, auto_reconnect=True)

        async def always_fail(message, timeout_s):
            raise ConnectionError("permanent failure")

        client._send_request = always_fail
        with pytest.raises(ConnectionError, match="failed"):
            await client.execute_code("print('fail')")


# =========================================================================
# Fail pending
# =========================================================================

class TestFailPending:
    @pytest.mark.asyncio
    async def test_fail_pending_sets_exceptions(self):
        client = _make_client()
        loop = asyncio.get_event_loop()

        f1 = loop.create_future()
        f2 = loop.create_future()
        client._pending_requests["a"] = f1
        client._pending_requests["b"] = f2

        client._fail_pending(ConnectionError("test"))

        assert len(client._pending_requests) == 0
        with pytest.raises(ConnectionError):
            f1.result()
        with pytest.raises(ConnectionError):
            f2.result()

    @pytest.mark.asyncio
    async def test_fail_pending_skips_done_futures(self):
        client = _make_client()
        loop = asyncio.get_event_loop()

        f = loop.create_future()
        f.set_result({"ok": True})
        client._pending_requests["a"] = f

        client._fail_pending(ConnectionError("test"))
        # Should not raise, future was already done
        assert f.result() == {"ok": True}


# =========================================================================
# Global client management
# =========================================================================

class TestGlobalClient:
    @pytest.mark.asyncio
    async def test_get_and_close(self):
        client = await get_bridge_client()
        assert client.connected

        await close_bridge_client()
        # After close, getting a new client should create fresh one
        client2 = await get_bridge_client()
        assert client2.connected
        await close_bridge_client()
