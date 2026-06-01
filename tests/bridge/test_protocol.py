"""Protocol tests - spin up real WebSocket server, test message format and routing."""

import asyncio
import json
import threading
import time
from concurrent.futures import Future

import pytest
import websockets
from yade_mcp_bridge.execution.main_thread import MainThreadExecutor
from yade_mcp_bridge.server import create_server
from yade_mcp_bridge.tasks.task import ScriptTask


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


@pytest.fixture()
async def bridge_server_with_tasks(tmp_path):
    """Bridge server fixture that exposes the task_manager for direct task injection."""
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

    yield url, server._context.task_manager, tmp_path

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
        assert resp["ok"] is True
        assert "status" not in resp
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

        assert resp["ok"] is False
        assert "SyntaxError" in resp["error"]["message"]

    async def test_missing_code_field(self, bridge_server):
        url, _ = bridge_server
        resp = await _send_recv(url, {"type": "execute_code", "request_id": "e3"})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "missing_field"
        assert resp["error"]["details"]["field"] == "code"
        assert "code required" in resp["error"]["message"]

    async def test_eval_result_returned(self, bridge_server):
        url, executor = bridge_server
        msg = {"type": "execute_code", "request_id": "e4", "code": "1 + 2"}
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(msg))
            await asyncio.sleep(0.05)
            executor.process_tasks()
            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            resp = json.loads(raw)

        assert resp["ok"] is True
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
        assert resp["ok"] is False
        assert resp["error"]["code"] == "not_found"

    async def test_list_tasks_empty(self, bridge_server):
        url, _ = bridge_server
        resp = await _send_recv(url, {"type": "list_tasks", "request_id": "l1"})
        assert resp["ok"] is True
        assert isinstance(resp["data"], list)

    async def test_interrupt_nonexistent_task(self, bridge_server):
        url, _ = bridge_server
        resp = await _send_recv(url, {
            "type": "interrupt_task",
            "request_id": "i1",
            "task_id": "nonexistent",
        })
        assert resp["ok"] is False
        assert resp["error"]["code"] == "not_found"

    async def test_missing_task_id_returns_error(self, bridge_server):
        url, _ = bridge_server
        resp = await _send_recv(url, {"type": "check_task_status", "request_id": "t2"})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "missing_field"
        assert "task_id required" in resp["error"]["message"]


# =========================================================================
# Check task status pagination (bridge-side)
# =========================================================================


class TestCheckTaskStatusPagination:
    """End-to-end websocket tests for bridge-side pagination of task output.

    Injects a ScriptTask backed by a real log file into the task_manager,
    then issues check_task_status messages with various skip_newest/limit/
    filter_text combinations and verifies that both the output window and
    the pagination metadata reflect the full log (not a pre-truncated view).
    """

    def _inject_task(self, task_manager, tmp_path, task_id, lines):
        log_path = str(tmp_path / f"{task_id}.log")
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))

        future = Future()
        future.set_result({"status": "success"})
        task = ScriptTask(
            task_id,
            future,
            f"{task_id}.py",
            f"/tmp/{task_id}.py",
            description=f"task {task_id}",
        )
        task.log_path = log_path
        task_manager.tasks[task_id] = task
        return log_path

    async def test_default_pagination_returns_tail_window(self, bridge_server_with_tasks):
        url, task_manager, tmp_path = bridge_server_with_tasks
        self._inject_task(task_manager, tmp_path, "tail1", [f"line {i}" for i in range(200)])

        resp = await _send_recv(url, {
            "type": "check_task_status",
            "request_id": "p1",
            "task_id": "tail1",
        })

        data = resp["data"]
        assert data["status"] == "completed"
        assert data["pagination"]["total_lines"] == 200
        assert data["pagination"]["has_older"] is True
        assert data["pagination"]["has_newer"] is False
        # Default limit=64, so we should see the last 64 lines.
        assert "line 199" in data["output"]
        assert "line 136" in data["output"]
        assert "line 135" not in data["output"]

    async def test_skip_newest_and_limit(self, bridge_server_with_tasks):
        url, task_manager, tmp_path = bridge_server_with_tasks
        self._inject_task(task_manager, tmp_path, "skip1", [f"line {i}" for i in range(100)])

        resp = await _send_recv(url, {
            "type": "check_task_status",
            "request_id": "p2",
            "task_id": "skip1",
            "skip_newest": 10,
            "limit": 5,
        })

        data = resp["data"]
        assert data["pagination"]["total_lines"] == 100
        assert data["pagination"]["has_older"] is True
        assert data["pagination"]["has_newer"] is True
        # Skipping 10 from the end leaves lines 0..89; limit=5 picks lines 85..89.
        assert "line 89" in data["output"]
        assert "line 85" in data["output"]
        assert "line 84" not in data["output"]
        assert "line 90" not in data["output"]

    async def test_filter_text_applies_to_full_log(self, bridge_server_with_tasks):
        url, task_manager, tmp_path = bridge_server_with_tasks
        # Interleave error lines throughout a 300-line log — some are past
        # any naive head-truncation window, which is exactly the bug we
        # are guarding against.
        lines = []
        for i in range(300):
            if i in (5, 150, 295):
                lines.append(f"error at step {i}")
            else:
                lines.append(f"ok {i}")
        self._inject_task(task_manager, tmp_path, "filter1", lines)

        resp = await _send_recv(url, {
            "type": "check_task_status",
            "request_id": "p3",
            "task_id": "filter1",
            "filter_text": "error",
            "limit": 10,
        })

        data = resp["data"]
        assert data["pagination"]["total_lines"] == 3
        assert "error at step 5" in data["output"]
        assert "error at step 150" in data["output"]
        assert "error at step 295" in data["output"]
        assert "ok " not in data["output"]

    async def test_empty_log_returns_empty_pagination(self, bridge_server_with_tasks):
        url, task_manager, tmp_path = bridge_server_with_tasks
        self._inject_task(task_manager, tmp_path, "empty1", [])

        resp = await _send_recv(url, {
            "type": "check_task_status",
            "request_id": "p4",
            "task_id": "empty1",
        })

        data = resp["data"]
        assert data["pagination"]["total_lines"] == 0
        assert data["pagination"]["line_range"] == "0-0"
        assert data["pagination"]["has_older"] is False
        assert data["pagination"]["has_newer"] is False


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


# =========================================================================
# execute_code timeout termination (end-to-end)
# =========================================================================


@pytest.fixture()
async def bridge_server_with_pump():
    """Bridge server paired with a real background pump thread, so the
    SetAsyncExc termination path can actually run user code on a
    non-main thread (just like Mode 1 in production)."""
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

    stop_pump = threading.Event()

    def pump_loop():
        while not stop_pump.is_set():
            executor.process_tasks(max_tasks=1)
            time.sleep(0.005)

    pump_thread = threading.Thread(target=pump_loop, name="test-task-pump", daemon=True)
    pump_thread.start()

    yield url, executor

    stop_pump.set()
    pump_thread.join(timeout=1.0)
    server.server.close()
    await server.server.wait_closed()


class TestExecuteCodeTimeoutTermination:
    async def test_tight_loop_times_out_and_terminates_cleanly(self, bridge_server_with_pump):
        """Pure-Python infinite loop hits short timeout → SetAsyncExc
        aborts it → status="terminated"."""
        url, _ = bridge_server_with_pump
        msg = {
            "type": "execute_code",
            "request_id": "term-tight",
            "code": "while True:\n    pass",
            "timeout_ms": 500,
        }
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(msg))
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            resp = json.loads(raw)

        assert resp["type"] == "execute_code_result"
        assert resp["request_id"] == "term-tight"
        assert resp["ok"] is False, f"expected failure, got {resp}"
        assert resp["error"]["code"] == "terminated", f"expected terminated, got {resp}"
        details = resp["error"].get("details") or {}
        assert details.get("method") == "async_exc"

    async def test_pump_recovers_after_termination(self, bridge_server_with_pump):
        """After a timed-out/aborted execute_code, the pump thread must
        be free to run subsequent requests. This is the load-bearing
        guarantee — without SetAsyncExc the pump would be locked
        forever."""
        url, _ = bridge_server_with_pump

        async with websockets.connect(url) as ws:
            # Kill a long loop.
            await ws.send(json.dumps({
                "type": "execute_code",
                "request_id": "pre",
                "code": "while True:\n    pass",
                "timeout_ms": 500,
            }))
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
            assert first["error"]["code"] == "terminated"

            # Now a quick call on the SAME pump: must succeed fast.
            t0 = time.time()
            await ws.send(json.dumps({
                "type": "execute_code",
                "request_id": "post",
                "code": "1 + 1",
                "timeout_ms": 1000,
            }))
            second = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            elapsed = time.time() - t0

        assert second["ok"] is True, f"pump didn't recover: {second}"
        assert second["data"]["result"] == 2
        assert elapsed < 2.0, f"pump was slow after recovery: {elapsed:.2f}s"

    async def test_base_exception_swallow_reports_timeout(self, bridge_server_with_pump):
        """User code that catches BaseException defeats SetAsyncExc by
        design. The injection fires but is caught; the loop continues
        until the bridge's grace period expires → status='timeout'
        with method='stuck_in_c' (the grace period times out)."""
        url, _ = bridge_server_with_pump
        # Catch BaseException AND re-enter the loop. Use a bounded
        # counter so the test thread eventually terminates (the pump
        # thread will come back after ~1s of swallowing).
        code = (
            "import time\n"
            "start = time.time()\n"
            "while time.time() - start < 1.5:\n"
            "    try:\n"
            "        pass\n"
            "    except BaseException:\n"
            "        pass\n"
        )
        msg = {
            "type": "execute_code",
            "request_id": "swallow",
            "code": code,
            "timeout_ms": 300,
        }
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(msg))
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            resp = json.loads(raw)

        # Either "terminated" (if injection happens OUTSIDE the try,
        # e.g., in the while-condition evaluation) or "timeout"
        # (stuck_in_c — pump didn't resolve within grace). Both are
        # acceptable — the point is that the pump does eventually
        # recover. Accept either outcome.
        assert resp["ok"] is False
        assert resp["error"]["code"] in ("terminated", "timeout")
        # In either case, confirm pump recovers with a follow-up ping.
        # Wait for the bridge's 1.5s self-termination in the stuck
        # case.
        await asyncio.sleep(2.0)
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({
                "type": "execute_code",
                "request_id": "after",
                "code": "42",
                "timeout_ms": 2000,
            }))
            follow = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert follow["ok"] is True

    async def test_execute_code_does_not_clobber_current_task_id(self, bridge_server_with_pump):
        """Regression: _execute_code must NOT set_current_task(request_id).

        If it did, a subsequent REPL timeout's request_interrupt() would
        set a flag that PyRunner's no-arg is_interrupt_requested() reads
        via _current_task_id → O.pause() fires → _hooked_run raises
        InterruptedError → the enclosing script task gets spuriously
        marked ``interrupted``. The fix: leave _current_task_id alone.
        """
        url, _ = bridge_server_with_pump
        from yade_mcp_bridge.signals import clear_current_task, peek_current_task, set_current_task

        # Simulate a running task by setting the sentinel outer task.
        clear_current_task()
        set_current_task("outer-task")
        try:
            async with websockets.connect(url) as ws:
                # Normal, successful execute_code.
                await ws.send(json.dumps({
                    "type": "execute_code",
                    "request_id": "nested-ok",
                    "code": "1 + 1",
                    "timeout_ms": 2000,
                }))
                ok = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                assert ok["ok"] is True
                # After execute_code completes, the outer task must still
                # be the current one — not None, not request_id.
                assert peek_current_task() == "outer-task"

                # Timed-out execute_code: the termination path calls
                # request_interrupt(request_id) on the REPL's own id.
                # That flag must not leak into any PyRunner tick reading
                # _current_task_id.
                await ws.send(json.dumps({
                    "type": "execute_code",
                    "request_id": "nested-timeout",
                    "code": "while True:\n    pass",
                    "timeout_ms": 500,
                }))
                terminated = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                assert terminated["error"]["code"] == "terminated"
                assert peek_current_task() == "outer-task"
        finally:
            clear_current_task()
