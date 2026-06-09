"""Shared test fixtures.

Provides an in-process bridge server fixture so the MCP client can be
tested without a YADE runtime or the ``yade_mcp_bridge`` package. It
speaks the same JSON-over-HTTP protocol as the real bridge (``POST
/<command>`` for request/response plus a ``GET /events`` SSE stream); the
response shapes mirror ``yade_mcp_bridge/handlers`` and are pinned against
the live server in the bridge's own ``tests/bridge/test_protocol.py``.
"""

import http.server
import json
import socketserver
import threading

import pytest


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
                "offset": 0,
                "limit": None,
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


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _FakeBridgeHandler(http.server.BaseHTTPRequestHandler):
    """Stdlib HTTP handler mirroring the real bridge's transport.

    ``POST /<command>`` routes through ``_build_response``; ``GET /events``
    holds an SSE connection open so the client's background consumer (started
    on ``connect()``) stays connected.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        command = self.path.split("?", 1)[0].strip("/")
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""

        try:
            request = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send_json(
                400,
                {"type": "error", "ok": False, "error": {"code": "invalid_json", "message": "Invalid JSON format"}},
            )
            return

        request.setdefault("type", command)
        response = _build_response(request)
        if response is None:
            self._send_json(
                404,
                {"type": "error", "ok": False, "error": {"code": "unknown_command", "message": command}},
            )
            return
        self._send_json(200, response)

    def do_GET(self):
        if self.path.split("?", 1)[0] != "/events":
            self._send_json(404, {"ok": False, "error": {"code": "not_found", "message": "Not found"}})
            return
        stop = self.server.stop_event
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while not stop.is_set():
                if stop.wait(0.5):
                    break
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            pass


@pytest.fixture()
def bridge_server():
    """Start an in-process bridge server on an ephemeral port.

    Yields the ``http://`` base URL the client should connect to.
    """
    httpd = _ThreadingHTTPServer(("127.0.0.1", 0), _FakeBridgeHandler)
    httpd.stop_event = threading.Event()
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.stop_event.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2.0)
