"""YADE bridge HTTP + SSE server - runs inside the YADE Python environment.

The transport is stdlib only (``http.server`` + Server-Sent Events): no
third-party dependency and no asyncio. Each request is served on its own
thread (``ThreadingMixIn``), so a long-blocking ``execute_code`` never stalls
a concurrent status query. Request/response is plain ``POST /<command>``;
the two server->client doorbells (``task_status_changed``, ``console_entry``)
are pushed over a single long-lived ``GET /events`` SSE stream.
"""

import http.server
import json
import logging
import queue
import socketserver
import threading
import time

from .console import ConsoleHistory
from .execution import ScriptRunner
from .handlers import (
    ServerContext,
    handle_check_task_status,
    handle_console_history,
    handle_execute_code,
    handle_interrupt_task,
    handle_list_tasks,
    handle_yade_task,
)
from .tasks import TaskManager

logger = logging.getLogger("YADE-Bridge")

# SSE keepalive: how long an idle /events stream waits before emitting a
# comment line. Keeps the connection (and any intermediary proxy) alive and
# replaces WebSocket ping/pong.
_SSE_KEEPALIVE_S = 15.0

# Per-client doorbell queue depth. Doorbells are payload-free signals and the
# client always re-polls status, so a bounded queue that drops on overflow is
# safe: it can never block the simulation thread or a script thread.
_SSE_QUEUE_MAXSIZE = 256


def _json_bytes(obj):
    return json.dumps(obj).encode("utf-8")


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Thread-per-request HTTP server.

    ``ThreadingMixIn`` is required: the long-lived ``GET /events`` SSE stream
    parks a thread for the connection's lifetime, so a single-threaded server
    would block every other request behind it. This is a 3.6-safe shim for
    ``http.server.ThreadingHTTPServer`` (which only exists on 3.7+).
    """

    daemon_threads = True
    allow_reuse_address = True


class _BridgeRequestHandler(http.server.BaseHTTPRequestHandler):
    """Maps HTTP requests onto the transport-agnostic handler dict.

    ``POST /<command>`` dispatches to the request/response handler;
    ``GET /events`` serves the SSE doorbell stream. The owning
    ``YADEBridgeServer`` is reachable via ``self.server.bridge``.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Silence BaseHTTPRequestHandler's default stderr access log; the
        # bridge logs request/response summaries through its own logger.
        pass

    @property
    def _bridge(self):
        return self.server.bridge

    def _write_json(self, status, payload_bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload_bytes)))
        self.end_headers()
        try:
            self.wfile.write(payload_bytes)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def do_POST(self):
        command = self.path.split("?", 1)[0].strip("/")
        raw = self._read_body()

        handler = self._bridge.handlers.get(command)
        if handler is None:
            self._write_json(
                404,
                _json_bytes(
                    {
                        "type": "error",
                        "ok": False,
                        "error": {"code": "unknown_command", "message": f"Unknown command: {command}"},
                    }
                ),
            )
            return

        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError) as exc:
            self._write_json(
                400,
                _json_bytes(
                    {
                        "type": "error",
                        "ok": False,
                        "error": {
                            "code": "invalid_json",
                            "message": "Invalid JSON format",
                            "details": {"error": str(exc)},
                        },
                    }
                ),
            )
            return

        request_id = data.get("request_id", "unknown")
        summary = self._bridge.summarize_request(command, data)
        logger.info("[%s] >> %s %s", str(request_id)[:8], command, summary)

        t0 = time.time()
        try:
            response = handler(self._bridge.context, data)
        except Exception as exc:  # last-resort safety net; handlers build their own error envelopes
            logger.error("[%s] handler error: %s", str(request_id)[:8], exc)
            self._write_json(
                500,
                _json_bytes(
                    {
                        "type": "error",
                        "request_id": request_id,
                        "ok": False,
                        "error": {
                            "code": "internal_error",
                            "message": "Internal server error",
                            "details": {"error": str(exc)},
                        },
                    }
                ),
            )
            return
        elapsed_ms = (time.time() - t0) * 1000

        # Task/console handlers still carry a lifecycle ``status``; the
        # execute_code path signals via ``ok``. Derive a log token from
        # whichever the response uses.
        status = response.get("status")
        if status is None:
            status = "ok" if response.get("ok") else "error"
        logger.info("[%s] << %s status=%s (%.0fms)", str(request_id)[:8], command, status, elapsed_ms)

        self._write_json(200, self._bridge.serialize_response(response, request_id))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/events":
            self._serve_sse()
        elif path == "/health":
            self._serve_health()
        else:
            self._write_json(404, _json_bytes({"ok": False, "error": {"code": "not_found", "message": "Not found"}}))

    def _serve_health(self):
        """Liveness probe for curl / Docker HEALTHCHECK / pre-flight checks."""
        from . import __version__  # lazy: the package __init__ imports this module

        payload = {
            "ok": True,
            "version": __version__,
            "runtime_mode": self._bridge.context.runtime_mode,
        }
        self._write_json(200, _json_bytes(payload))

    def _serve_sse(self):
        q = self._bridge.register_sse_client()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=_SSE_KEEPALIVE_S)
                except queue.Empty:
                    # Keepalive comment line (ignored by the client parser).
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(b"data: " + msg.encode("utf-8") + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            # Client went away; fall through to deregister.
            pass
        finally:
            self._bridge.unregister_sse_client(q)


class YADEBridgeServer:
    """HTTP + SSE bridge for YADE script execution via the main-thread queue."""

    _MAX_RESPONSE_BYTES = 40 * 2**20  # 40 MB output safety net

    # Tail of ``data["output"]`` kept when a response blows past the byte cap.
    # ~10k chars is a few thousand LLM tokens: enough to show the most recent
    # progress/error, small enough not to flood the agent's context.
    _TRUNCATED_TAIL_CHARS = 10000

    def __init__(
        self,
        main_executor,
        host="localhost",
        port=9002,
        runtime_mode="unknown",
        max_tasks=None,
    ):
        self.host = host
        self.port = port

        # Registry of connected SSE clients: one bounded queue per client.
        # Mutated from SSE request threads (connect/disconnect) and snapshotted
        # by the broadcast path, so all access is guarded by ``_conn_lock``.
        self.active_connections = set()
        self._conn_lock = threading.Lock()

        tm_kwargs = {"on_task_terminal": self._broadcast_task_status}
        if max_tasks is not None:
            tm_kwargs["max_tasks"] = max_tasks
        task_manager = TaskManager(**tm_kwargs)

        console_history = ConsoleHistory()
        console_history.on_new_entry = self._broadcast_console_entry

        # Single home for handler dependencies; handlers and external callers
        # reach them via ``self.context``, never as server attributes.
        self.context = ServerContext(
            task_manager=task_manager,
            script_runner=ScriptRunner(main_executor, task_manager),
            main_executor=main_executor,
            runtime_mode=runtime_mode,
            console_history=console_history,
        )

        self.handlers = {
            "yade_task": handle_yade_task,
            "check_task_status": handle_check_task_status,
            "list_tasks": handle_list_tasks,
            "interrupt_task": handle_interrupt_task,
            "execute_code": handle_execute_code,
            "console_history": handle_console_history,
        }

        # Bind eagerly so a port conflict surfaces on the calling (main)
        # thread, before the serving thread starts.
        self._httpd = _ThreadingHTTPServer((host, port), _BridgeRequestHandler)
        self._httpd.bridge = self

    # -- SSE client registry -------------------------------------------------

    def register_sse_client(self):
        q = queue.Queue(maxsize=_SSE_QUEUE_MAXSIZE)
        with self._conn_lock:
            self.active_connections.add(q)
            total = len(self.active_connections)
        logger.info("SSE client connected (total=%d)", total)
        return q

    def unregister_sse_client(self, q):
        with self._conn_lock:
            self.active_connections.discard(q)
            total = len(self.active_connections)
        logger.info("SSE client disconnected (total=%d)", total)

    # -- Server->client doorbells -------------------------------------------

    def _broadcast(self, payload):
        """Push a payload-free doorbell to every connected SSE client.

        Snapshot the queue set under the lock, then ``put_nowait`` outside it
        (never hold the lock across a put). Called from several threads: the
        ``script-<id>`` daemon thread and POST worker threads
        (``task_status_changed``) and the main thread (``console_entry``).
        ``queue.Queue`` is thread-safe; on overflow the doorbell is dropped
        because the client always re-polls status.
        """
        with self._conn_lock:
            if not self.active_connections:
                return
            queues = list(self.active_connections)
        msg = json.dumps(payload)
        for q in queues:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    def _broadcast_task_status(self, task_id, status):
        self._broadcast({"type": "task_status_changed", "task_id": task_id, "status": status})

    def _broadcast_console_entry(self, entry):
        self._broadcast({"type": "console_entry", "entry_id": entry.get("id")})

    # -- Response serialization ---------------------------------------------

    def serialize_response(self, response, request_id="unknown"):
        payload = json.dumps(response)
        if len(payload) > self._MAX_RESPONSE_BYTES:
            logger.warning("[%s] Response too large (%d bytes), truncating output", str(request_id)[:8], len(payload))
            response = self._truncate_response(response)
            payload = json.dumps(response)
        return payload.encode("utf-8")

    @classmethod
    def _truncate_response(cls, response):
        """Truncate large response data to fit within transport limits.

        Keeps the TAIL of output (most recent content), since monitoring
        scenarios care about current progress, not the start of a long log.
        For task responses this is a defensive safety net - bridge-side
        pagination already limits output before it reaches this path.
        """
        data = response.get("data", {})
        if isinstance(data, dict) and "output" in data:
            output = data["output"]
            if isinstance(output, str) and len(output) > cls._TRUNCATED_TAIL_CHARS:
                tail = output[-cls._TRUNCATED_TAIL_CHARS :]
                nl = tail.find("\n")
                if nl >= 0:
                    tail = tail[nl + 1 :]
                omitted = len(output) - len(tail)
                data["output"] = (
                    f"... [TRUNCATED: {omitted} earlier chars omitted, "
                    f"showing most recent {len(tail)} chars. "
                    f"Consider writing output to file instead of printing.]\n"
                ) + tail
                response["data"] = data
        return response

    def summarize_request(self, command, data):
        """Build a short log summary for an incoming request."""
        if command == "execute_code":
            code = data.get("code", "")
            preview = code[:80].replace("\n", "\\n")
            if len(code) > 80:
                preview += "..."
            return f'code="{preview}"'
        if command == "yade_task":
            return f'script="{data.get("script_path", "?")}" desc="{data.get("description", "")[:60]}"'
        if command in ("check_task_status", "interrupt_task"):
            return f"task_id={data.get('task_id', '?')}"
        if command == "list_tasks":
            return f"offset={data.get('offset', 0)} limit={data.get('limit', 'all')}"
        return ""

    # -- Lifecycle ----------------------------------------------------------

    def serve_forever(self):
        logger.info("HTTP bridge listening on http://%s:%d", self.host, self.port)
        self._httpd.serve_forever()

    def shutdown(self):
        """Graceful shutdown: stop the HTTP server and flush task data.

        ``HTTPServer.shutdown()`` must be called from a thread other than the
        one running ``serve_forever`` - here the main-thread atexit/signal
        handler, while serving runs on a daemon thread.
        """
        self.context.task_manager.shutdown()
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        try:
            self._httpd.server_close()
        except Exception:
            pass
        logger.info("Server shutdown complete")

    def set_runtime_mode(self, runtime_mode):
        self.context.runtime_mode = runtime_mode


def create_server(
    main_executor,
    host="localhost",
    port=9002,
    runtime_mode="unknown",
    max_tasks=None,
):
    return YADEBridgeServer(
        main_executor,
        host,
        port,
        runtime_mode=runtime_mode,
        max_tasks=max_tasks,
    )
