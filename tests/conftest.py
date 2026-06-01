"""Shared test fixtures.

Provides an in-process bridge server fixture so the MCP client can be
tested without a YADE runtime or the ``yade_mcp_bridge`` package. It
speaks the same JSON-over-WebSocket protocol as the real bridge; the
response shapes mirror ``yade_mcp_bridge/handlers`` and are pinned
against the live server in the bridge's own ``tests/bridge/test_protocol.py``.
"""

import json

import pytest
import websockets


def _err(response_type, request_id, code, message, *, details=None, data=None):
    """Failure wire message mirroring the bridge's ``error_response``.

    Local copy (the stub must not import ``yade_mcp_bridge``); kept pinned to
    the bridge's shape by ``tests/bridge/test_protocol.py``.
    """
    error = {"code": code, "message": message}
    if details:
        error["details"] = details
    resp = {"type": response_type, "request_id": request_id, "ok": False, "error": error}
    if data is not None:
        resp["data"] = data
    return resp


def _build_response(request):
    """Map a request message to the bridge's response shape.

    Returns ``None`` for unknown message types, mirroring the real
    server, which logs a warning and sends nothing.
    """
    msg_type = request.get("type")
    request_id = request.get("request_id", "unknown")

    if msg_type == "ping":
        return {
            "type": "result",
            "request_id": request_id,
            "status": "success",
            "message": "pong",
            "data": {"runtime_mode": "test"},
        }

    if msg_type == "execute_code":
        if not request.get("code"):
            return _err(
                "execute_code_result",
                request_id,
                "missing_field",
                "code required",
                details={"field": "code"},
            )
        # Success: ok-envelope (no top-level status/message), matching the
        # tightened execute_code wire.
        return {
            "type": "execute_code_result",
            "request_id": request_id,
            "ok": True,
            "data": {"output": "(ok)"},
        }

    if msg_type == "check_task_status":
        if not request.get("task_id"):
            return _err("result", request_id, "missing_field", "task_id required", details={"field": "task_id"})
        return _err(
            "result",
            request_id,
            "not_found",
            "Task ID not found: {}".format(request["task_id"]),
        )

    if msg_type == "list_tasks":
        return {
            "type": "result",
            "request_id": request_id,
            "ok": True,
            "data": [],
            "pagination": {
                "total_count": 0,
                "displayed_count": 0,
                "offset": 0,
                "limit": None,
                "has_more": False,
            },
        }

    if msg_type == "interrupt_task":
        if not request.get("task_id"):
            return _err("result", request_id, "missing_field", "task_id required", details={"field": "task_id"})
        return _err(
            "result",
            request_id,
            "not_found",
            "Task not found: {}".format(request["task_id"]),
            data={"task_id": request["task_id"], "interrupt_requested": False},
        )

    if msg_type == "console_history":
        return {
            "type": "console_history_result",
            "request_id": request_id,
            "status": "success",
            "message": "",
            "data": {"entries": []},
        }

    return None


@pytest.fixture()
async def bridge_server():
    """Start an in-process bridge server on an ephemeral port.

    Yields the ``ws://`` URL the client should connect to.
    """

    async def handler(websocket):
        try:
            async for raw in websocket:
                try:
                    request = json.loads(raw)
                except json.JSONDecodeError as exc:
                    await websocket.send(
                        json.dumps({"type": "error", "message": "Invalid JSON format", "error": str(exc)})
                    )
                    continue
                response = _build_response(request)
                if response is not None:
                    await websocket.send(json.dumps(response))
        except websockets.exceptions.ConnectionClosed:
            pass

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    url = f"ws://127.0.0.1:{port}"

    yield url

    server.close()
    await server.wait_closed()
