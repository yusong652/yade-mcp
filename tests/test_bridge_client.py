"""Tests for bridge client: connection lifecycle, error handling, retries.

Uses the in-process ``bridge_server`` fixture (see ``conftest.py``), so
no YADE runtime or ``yade_mcp_bridge`` package is needed.
"""

from unittest.mock import AsyncMock

import httpx
import pytest

from yade_mcp.bridge.client import (
    YADEBridgeClient,
    close_bridge_client,
    get_bridge_client,
)


def _make_client(url="http://localhost:9002", **overrides):
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
        http1 = client._client
        await client.connect()  # should not reconnect
        assert client._client is http1
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
        assert result.get("ok") is True
        await client.disconnect()

    async def test_check_task_status_not_found(self, bridge_server):
        client = _make_client(bridge_server)
        await client.connect()
        result = await client.check_task_status("nonexistent-id")
        assert result["ok"] is False
        assert result["error"]["code"] == "not_found"
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
        assert result["ok"] is False
        assert result["error"]["code"] == "not_found"
        await client.disconnect()


# =========================================================================
# Timeout and retry
# =========================================================================


class TestTimeoutAndRetry:
    async def test_timeout_raises(self, bridge_server):
        client = _make_client(bridge_server, auto_reconnect=False, max_retries=0)
        await client.connect()

        # Force the POST to time out; the client converts httpx timeouts to a
        # TimeoutError, which _request_with_retry surfaces as ConnectionError.
        client._client.post = AsyncMock(side_effect=httpx.ReadTimeout("simulated"))

        with pytest.raises(ConnectionError, match="failed"):
            await client.execute_code("print('slow')", timeout_ms=1)
        await client.disconnect()

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
        await client.execute_code("print('recovered')")
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
