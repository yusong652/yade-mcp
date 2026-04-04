"""Tests for bridge client: connection lifecycle, error handling, retries.

Uses a real bridge WebSocket server (no YADE runtime needed).
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from yade_mcp.bridge.client import (
    BridgeResponseTooLargeError,
    YADEBridgeClient,
    close_bridge_client,
    get_bridge_client,
)
from yade_mcp_bridge.execution.main_thread import MainThreadExecutor
from yade_mcp_bridge.server import create_server


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture()
async def bridge_server():
    """Start a real bridge WebSocket server + background task processor."""
    executor = MainThreadExecutor()
    server = create_server(
        main_executor=executor,
        host="127.0.0.1",
        port=0,
        ping_interval=None,
        ping_timeout=None,
        runtime_mode="test",
    )
    await server.start()
    port = server.server.sockets[0].getsockname()[1]
    url = f"ws://127.0.0.1:{port}"

    # Background task to process executor queue (simulates main thread pump)
    stop = asyncio.Event()

    async def pump():
        while not stop.is_set():
            executor.process_tasks()
            await asyncio.sleep(0.01)

    pump_task = asyncio.create_task(pump())

    yield url

    stop.set()
    pump_task.cancel()
    try:
        await pump_task
    except asyncio.CancelledError:
        pass
    server.server.close()
    await server.server.wait_closed()


def _make_client(url="ws://localhost:9002", **overrides):
    defaults = dict(
        url=url,
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
    async def test_connect_and_disconnect(self, bridge_server):
        client = _make_client(bridge_server)
        assert not client.connected

        await client.connect()
        assert client.connected

        await client.disconnect()
        assert not client.connected

    async def test_double_connect_is_noop(self, bridge_server):
        client = _make_client(bridge_server)
        await client.connect()
        ws1 = client._websocket
        await client.connect()  # should not reconnect
        assert client._websocket is ws1
        await client.disconnect()

    async def test_disconnect_when_not_connected(self, bridge_server):
        client = _make_client(bridge_server)
        await client.disconnect()  # should not raise
        assert not client.connected

    async def test_ensure_connected(self, bridge_server):
        client = _make_client(bridge_server)
        await client._ensure_connected()
        assert client.connected
        await client.disconnect()


# =========================================================================
# Request/response
# =========================================================================


class TestRequests:
    async def test_execute_code(self, bridge_server):
        client = _make_client(bridge_server)
        await client.connect()
        result = await client.execute_code("print('test')")
        assert result.get("status") in ("ok", "success")
        await client.disconnect()

    async def test_check_task_status_not_found(self, bridge_server):
        client = _make_client(bridge_server)
        await client.connect()
        result = await client.check_task_status("nonexistent-id")
        assert result.get("status") == "not_found"
        await client.disconnect()

    async def test_list_tasks(self, bridge_server):
        client = _make_client(bridge_server)
        await client.connect()
        result = await client.list_tasks(offset=0, limit=5)
        assert "data" in result or "status" in result
        await client.disconnect()

    async def test_interrupt_nonexistent(self, bridge_server):
        client = _make_client(bridge_server)
        await client.connect()
        result = await client.interrupt_task("nonexistent-id")
        assert "status" in result
        await client.disconnect()


# =========================================================================
# Timeout and retry
# =========================================================================


class TestTimeoutAndRetry:
    async def test_timeout_raises(self, bridge_server):
        client = _make_client(bridge_server, request_timeout_s=0.001, auto_reconnect=False, max_retries=0)
        await client.connect()

        # Patch send to not actually send (so no response comes back -> timeout)
        client._websocket.send = AsyncMock()

        with pytest.raises(ConnectionError, match="failed"):
            await client.execute_code("print('slow')", timeout_ms=1)

    async def test_retry_on_failure(self, bridge_server):
        client = _make_client(bridge_server, max_retries=2, reconnect_interval_s=0.01, auto_reconnect=True)

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

    async def test_retry_exhausted(self, bridge_server):
        client = _make_client(bridge_server, max_retries=1, reconnect_interval_s=0.01, auto_reconnect=True)

        async def always_fail(message, timeout_s):
            raise ConnectionError("permanent failure")

        client._send_request = always_fail
        with pytest.raises(ConnectionError, match="failed"):
            await client.execute_code("print('fail')")


# =========================================================================
# Fail pending
# =========================================================================


class TestFailPending:
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
    async def test_get_and_close(self, bridge_server, monkeypatch):
        # Point the global client factory at our test server
        monkeypatch.setenv("YADE_MCP_BRIDGE_URL", bridge_server)

        # Reset global state
        import yade_mcp.bridge.client as client_mod
        monkeypatch.setattr(client_mod, "_client", None)

        client = await get_bridge_client()
        assert client.connected

        await close_bridge_client()
        # After close, getting a new client should create fresh one
        client2 = await get_bridge_client()
        assert client2.connected
        await close_bridge_client()
