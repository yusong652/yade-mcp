"""YADE WebSocket Server - Runs inside YADE Python environment."""

import asyncio
import json
import logging

import websockets

from .execution import ScriptRunner
from .handlers import (
    ServerContext,
    handle_check_task_status,
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

    def __init__(self, main_executor, host="localhost", port=9002,
                 ping_interval=20, ping_timeout=40, runtime_mode="unknown",
                 max_tasks=None):
        self.main_executor = main_executor
        self.host = host
        self.port = port
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        task_manager = TaskManager(max_tasks=max_tasks) if max_tasks is not None else TaskManager()
        self.script_runner = ScriptRunner(main_executor, task_manager)
        self.active_connections = set()
        self.server = None
        self._loop = None

        self._context = ServerContext(
            task_manager=task_manager,
            script_runner=self.script_runner,
            main_executor=self.main_executor,
            runtime_mode=runtime_mode,
        )

        self._handlers = {
            "yade_task": handle_yade_task,
            "check_task_status": handle_check_task_status,
            "list_tasks": handle_list_tasks,
            "interrupt_task": handle_interrupt_task,
            "execute_code": handle_execute_code,
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
        """Truncate large response data to fit within WebSocket limits."""
        data = response.get("data", {})
        if isinstance(data, dict) and "output" in data:
            output = data["output"]
            if isinstance(output, str) and len(output) > 10000:
                data["output"] = output[:10000] + (
                    f"\n\n... [TRUNCATED: output was {len(output)} chars, "
                    f"exceeds WebSocket size limit. "
                    f"Consider writing output to file instead of printing.]"
                )
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

                status = response.get("status", "?")
                logger.info("[%s] << %s status=%s (%.0fms)", request_id[:8], msg_type, status, elapsed_ms)

                await self._send_response(websocket, response, request_id)
            else:
                logger.warning("[%s] Unknown message type: %s", request_id[:8], msg_type)

        except json.JSONDecodeError as e:
            logger.error("Invalid JSON: %s", e)
            await self._send_response(websocket, {
                "type": "error",
                "message": "Invalid JSON format",
                "error": str(e)
            })
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[%s] Message handling error: %s", data.get("request_id", "?")[:8], e)
            await self._send_response(websocket, {
                "type": "error",
                "message": "Internal server error",
                "error": str(e)
            })

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


def create_server(main_executor, host="localhost", port=9002,
                  ping_interval=20, ping_timeout=40, runtime_mode="unknown",
                  max_tasks=None):
    return YADEWebSocketServer(
        main_executor, host, port, ping_interval, ping_timeout,
        runtime_mode=runtime_mode, max_tasks=max_tasks,
    )
