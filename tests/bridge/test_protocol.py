"""Protocol tests - spin up real WebSocket server, test message format and routing."""

import asyncio
import json

import pytest
import websockets

from yade_mcp_bridge.execution.main_thread import MainThreadExecutor
from yade_mcp_bridge.server import create_server


@pytest.fixture()
async def bridge_server():
    """Start a real bridge WebSocket server on an ephemeral port."""
    executor = MainThreadExecutor()
    server = create_server(
        main_executor=executor,
        host="127.0.0.1",
        port=0,  # OS picks a free port
        ping_interval=None,
        ping_timeout=None,
        runtime_mode="test",
    )
    await server.start()
    # Extract the actual port assigned by the OS
    port = server.server.sockets[0].getsockname()[1]
    url = "ws://127.0.0.1:{}".format(port)

    yield url, executor

    server.server.close()
    await server.server.wait_closed()


async def _send_recv(url, message):
    """Send a JSON message and return the parsed response."""
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps(message))
        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        return json.loads(raw)


# =========================================================================
# Ping
# =========================================================================


class TestPingProtocol:
    async def test_ping_response_shape(self, bridge_server):
        url, _ = bridge_server
        resp = await _send_recv(url, {"type": "ping", "request_id": "p1"})
        assert resp["type"] == "result"
        assert resp["request_id"] == "p1"
        assert resp["status"] == "success"
        assert resp["message"] == "pong"
        assert resp["data"]["runtime_mode"] == "test"


# =========================================================================
# Execute code
# =========================================================================


class TestExecuteCodeProtocol:
    async def test_success(self, bridge_server):
        url, executor = bridge_server
        msg = {"type": "execute_code", "request_id": "e1", "code": "print('hello')"}
        # Send the message
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(msg))
            # Process the task on the main thread
            # Give server time to queue the task
            await asyncio.sleep(0.05)
            executor.process_tasks()
            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            resp = json.loads(raw)

        assert resp["type"] == "execute_code_result"
        assert resp["request_id"] == "e1"
        assert resp["status"] == "success"
        assert "hello" in resp["data"]["output"]

    async def test_syntax_error(self, bridge_server):
        url, executor = bridge_server
        msg = {"type": "execute_code", "request_id": "e2", "code": "def ("}
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(msg))
            await asyncio.sleep(0.05)
            executor.process_tasks()
            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            resp = json.loads(raw)

        assert resp["status"] == "error"
        assert "SyntaxError" in resp.get("message", "") or "SyntaxError" in resp.get("error", {}).get("message", "")

    async def test_missing_code_field(self, bridge_server):
        url, _ = bridge_server
        resp = await _send_recv(url, {"type": "execute_code", "request_id": "e3"})
        assert resp["status"] == "error"
        assert "code required" in resp["message"]

    async def test_eval_result_returned(self, bridge_server):
        url, executor = bridge_server
        msg = {"type": "execute_code", "request_id": "e4", "code": "1 + 2"}
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(msg))
            await asyncio.sleep(0.05)
            executor.process_tasks()
            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            resp = json.loads(raw)

        assert resp["status"] == "success"
        assert resp["data"]["result"] == 3


# =========================================================================
# Task operations
# =========================================================================


class TestTaskProtocol:
    async def test_check_nonexistent_task(self, bridge_server):
        url, _ = bridge_server
        resp = await _send_recv(url, {
            "type": "check_task_status",
            "request_id": "t1",
            "task_id": "nonexistent",
        })
        assert resp["status"] == "not_found"

    async def test_list_tasks_empty(self, bridge_server):
        url, _ = bridge_server
        resp = await _send_recv(url, {"type": "list_tasks", "request_id": "l1"})
        assert resp["status"] == "success"
        assert isinstance(resp["data"], list)

    async def test_interrupt_nonexistent_task(self, bridge_server):
        url, _ = bridge_server
        resp = await _send_recv(url, {
            "type": "interrupt_task",
            "request_id": "i1",
            "task_id": "nonexistent",
        })
        assert resp["status"] == "error"

    async def test_missing_task_id_returns_error(self, bridge_server):
        url, _ = bridge_server
        resp = await _send_recv(url, {"type": "check_task_status", "request_id": "t2"})
        assert resp["status"] == "error"
        assert "task_id required" in resp["message"]


# =========================================================================
# Error handling
# =========================================================================


class TestErrorHandling:
    async def test_invalid_json(self, bridge_server):
        url, _ = bridge_server
        async with websockets.connect(url) as ws:
            await ws.send("not json{{{")
            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            resp = json.loads(raw)
        assert resp["type"] == "error"
        assert "Invalid JSON" in resp["message"]

    async def test_unknown_message_type(self, bridge_server):
        """Unknown message types are silently ignored (no response)."""
        url, _ = bridge_server
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"type": "unknown_type", "request_id": "u1"}))
            # Server logs a warning but sends no response
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=0.5)


# =========================================================================
# Connection management
# =========================================================================


class TestConnectionManagement:
    async def test_multiple_messages_on_same_connection(self, bridge_server):
        url, _ = bridge_server
        async with websockets.connect(url) as ws:
            for i in range(3):
                await ws.send(json.dumps({"type": "ping", "request_id": "m{}".format(i)}))
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                resp = json.loads(raw)
                assert resp["request_id"] == "m{}".format(i)
                assert resp["status"] == "success"

    async def test_concurrent_connections(self, bridge_server):
        url, _ = bridge_server

        async def ping(client_id):
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps({"type": "ping", "request_id": client_id}))
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                return json.loads(raw)

        results = await asyncio.gather(ping("c1"), ping("c2"), ping("c3"))
        ids = {r["request_id"] for r in results}
        assert ids == {"c1", "c2", "c3"}
