"""YADE WebSocket Server - Runs inside YADE Python environment."""

import asyncio
import json
import logging

import websockets

from .console import ConsoleHistory
from .execution import ScriptRunner
from .handlers import (
    ServerContext,
    handle_check_task_status,
    handle_console_history,
    handle_execute_code,
    handle_interrupt_task,
    handle_list_tasks,
    handle_ping,
    handle_yade_task,
)
from .tasks import TaskManager

logger = logging.getLogger("YADE-Bridge")


class YADEWebSocketServer:
    """WebSocket server for YADE script execution via main thread queue."""

    def __init__(
        self,
        main_executor,
        host="localhost",
        port=9002,
        ping_interval=20,
        ping_timeout=40,
        runtime_mode="unknown",
        max_tasks=None,
    ):
        self.main_executor = main_executor
        self.host = host
        self.port = port
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.active_connections = set()
        self.server = None
        self._loop = None

        tm_kwargs = {"on_task_terminal": self._broadcast_task_status}
        if max_tasks is not None:
            tm_kwargs["max_tasks"] = max_tasks
        task_manager = TaskManager(**tm_kwargs)
        self.script_runner = ScriptRunner(main_executor, task_manager)

        self.console_history = ConsoleHistory()
        self.console_history.on_new_entry = self._broadcast_console_entry

        self._context = ServerContext(
            task_manager=task_manager,
            script_runner=self.script_runner,
            main_executor=self.main_executor,
            runtime_mode=runtime_mode,
            console_history=self.console_history,
        )

        self._handlers = {
            "yade_task": handle_yade_task,
            "check_task_status": handle_check_task_status,
            "list_tasks": handle_list_tasks,
            "interrupt_task": handle_interrupt_task,
            "execute_code": handle_execute_code,
            "console_history": handle_console_history,
            "ping": handle_ping,
        }

    _MAX_RESPONSE_BYTES = 40 * 2**20  # 40 MB safety margin (max_size is 50 MB)

    async def _send_response(self, websocket, response, request_id="unknown"):
        try:
            payload = json.dumps(response)
            if len(payload) > self._MAX_RESPONSE_BYTES:
                logger.warning("[%s] Response too large (%d bytes), truncating output", request_id[:8], len(payload))
                response = self._truncate_response(response, self._MAX_RESPONSE_BYTES)
                payload = json.dumps(response)
            await websocket.send(payload)
            return True
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Cannot send result, connection closed: %s", request_id)
            return False

    @staticmethod
    def _truncate_response(response, max_bytes):
        """Truncate large response data to fit within WebSocket limits.

        Keeps the TAIL of output (most recent content), since monitoring
        scenarios care about current progress, not the start of a long log.
        For task responses this is a defensive safety net — bridge-side
        pagination already limits output before it reaches this path.
        """
        data = response.get("data", {})
        if isinstance(data, dict) and "output" in data:
            output = data["output"]
            if isinstance(output, str) and len(output) > 10000:
                tail = output[-10000:]
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

    def _summarize_request(self, msg_type, data):
        """Build a short log summary for an incoming request."""
        if msg_type == "execute_code":
            code = data.get("code", "")
            preview = code[:80].replace("\n", "\\n")
            if len(code) > 80:
                preview += "..."
            return f'code="{preview}"'
        if msg_type == "yade_task":
            return f'script="{data.get("script_path", "?")}" desc="{data.get("description", "")[:60]}"'
        if msg_type in ("check_task_status", "interrupt_task"):
            return f"task_id={data.get('task_id', '?')}"
        if msg_type == "list_tasks":
            return f"offset={data.get('offset', 0)} limit={data.get('limit', 'all')}"
        return ""

    async def _process_message(self, websocket, message):
        try:
            data = json.loads(message)
            msg_type = data.get("type", "yade_task")
            request_id = data.get("request_id", "unknown")

            summary = self._summarize_request(msg_type, data)
            logger.info("[%s] >> %s %s", request_id[:8], msg_type, summary)

            handler = self._handlers.get(msg_type)
            if handler:
                import time

                t0 = time.time()
                response = await handler(self._context, data)
                elapsed_ms = (time.time() - t0) * 1000

                # Task/console handlers still carry a lifecycle ``status``;
                # the execute_code path now signals via ``ok``. Derive a log
                # token from whichever the response uses.
                status = response.get("status")
                if status is None:
                    status = "ok" if response.get("ok") else "error"
                logger.info("[%s] << %s status=%s (%.0fms)", request_id[:8], msg_type, status, elapsed_ms)

                await self._send_response(websocket, response, request_id)
            else:
                logger.warning("[%s] Unknown message type: %s", request_id[:8], msg_type)

        except json.JSONDecodeError as e:
            logger.error("Invalid JSON: %s", e)
            await self._send_response(websocket, {"type": "error", "message": "Invalid JSON format", "error": str(e)})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[%s] Message handling error: %s", data.get("request_id", "?")[:8], e)
            await self._send_response(websocket, {"type": "error", "message": "Internal server error", "error": str(e)})

    def _broadcast_task_status(self, task_id, status):
        """Broadcast task status change to all connected clients.

        Called from the YADE main thread (via Future callback), not the
        asyncio event loop thread, so we use run_coroutine_threadsafe.
        """
        if not self._loop or not self.active_connections:
            return

        msg = json.dumps(
            {
                "type": "task_status_changed",
                "task_id": task_id,
                "status": status,
            }
        )

        async def _send_all():
            for ws in list(self.active_connections):
                try:
                    await ws.send(msg)
                except Exception:
                    pass

        asyncio.run_coroutine_threadsafe(_send_all(), self._loop)

    def _broadcast_console_entry(self, entry):
        """Broadcast new console entry notification to all connected clients.

        Called from the main thread (IPython hook), so we use
        run_coroutine_threadsafe like _broadcast_task_status.
        """
        if not self._loop or not self.active_connections:
            return

        msg = json.dumps(
            {
                "type": "console_entry",
                "entry_id": entry.get("id"),
            }
        )

        async def _send_all():
            for ws in list(self.active_connections):
                try:
                    await ws.send(msg)
                except Exception:
                    pass

        asyncio.run_coroutine_threadsafe(_send_all(), self._loop)

    async def handle_client(self, websocket, path=None):
        remote = websocket.remote_address
        logger.info("Client connected: %s:%s (total=%d)", remote[0], remote[1], len(self.active_connections) + 1)
        self.active_connections.add(websocket)
        pending_tasks = set()

        try:
            async for message in websocket:
                task = asyncio.ensure_future(self._process_message(websocket, message))
                pending_tasks.add(task)
                task.add_done_callback(pending_tasks.discard)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            for task in pending_tasks:
                task.cancel()
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
            self.active_connections.discard(websocket)
            logger.info("Client disconnected: %s:%s (total=%d)", remote[0], remote[1], len(self.active_connections))

    async def start(self):
        self._loop = asyncio.get_event_loop()
        self.server = await websockets.serve(
            self.handle_client,
            self.host,
            self.port,
            ping_interval=self.ping_interval,
            ping_timeout=self.ping_timeout,
            max_size=50 * 2**20,
        )

    def shutdown(self):
        """Graceful shutdown: close WebSocket server and flush task data."""
        self._context.task_manager.shutdown()
        if self.server and self._loop and self._loop.is_running():
            try:
                self._loop.call_soon_threadsafe(self.server.close)
            except RuntimeError:
                pass
        logger.info("Server shutdown complete")

    def set_runtime_mode(self, runtime_mode):
        self._context.runtime_mode = runtime_mode


def create_server(
    main_executor,
    host="localhost",
    port=9002,
    ping_interval=20,
    ping_timeout=40,
    runtime_mode="unknown",
    max_tasks=None,
):
    return YADEWebSocketServer(
        main_executor,
        host,
        port,
        ping_interval,
        ping_timeout,
        runtime_mode=runtime_mode,
        max_tasks=max_tasks,
    )
