"""Protocol tests - spin up a real bridge HTTP + SSE server, test message format and routing."""

import asyncio
import threading
import time
from concurrent.futures import Future

import httpx
import pytest
from yade_mcp_bridge.execution.serial import SerialExecutor
from yade_mcp_bridge.tasks.task import ScriptTask
from yade_mcp_bridge.transport.server import create_server


def _start_bridge():
    """Create a bridge server on an ephemeral port, serving in a background
    thread with a real execute_code pump.

    The pump matters for HTTP: ``handle_execute_code`` blocks the request
    thread on the main-thread future, so a background pump must run the
    submitted code (just like Mode 1 in production) for the POST to resolve.
    """
    executor = SerialExecutor()
    server = create_server(executor=executor, host="127.0.0.1", port=0, runtime_mode="test")
    url = f"http://127.0.0.1:{server._httpd.server_address[1]}"

    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()

    stop_pump = threading.Event()

    def pump_loop():
        while not stop_pump.is_set():
            executor.run_next()
            time.sleep(0.005)

    pump_thread = threading.Thread(target=pump_loop, name="test-task-pump", daemon=True)
    pump_thread.start()

    def stop():
        stop_pump.set()
        pump_thread.join(timeout=1.0)
        server.shutdown()

    return server, executor, url, stop


@pytest.fixture()
def bridge_server():
    """A real bridge server (HTTP + SSE) with a background execute_code pump."""
    server, executor, url, stop = _start_bridge()
    try:
        yield url, executor
    finally:
        stop()


@pytest.fixture()
def bridge_server_with_tasks(tmp_path):
    """Bridge server that exposes the task_manager for direct task injection."""
    server, executor, url, stop = _start_bridge()
    try:
        yield url, server.context.task_manager, tmp_path
    finally:
        stop()


@pytest.fixture()
def bridge_server_with_pump():
    """Alias of ``bridge_server`` for the termination tests: a real pump on a
    non-main thread is required for the SetAsyncExc path to abort live code."""
    server, executor, url, stop = _start_bridge()
    try:
        yield url, executor
    finally:
        stop()


async def _send_recv(url, message, timeout=10.0):
    """POST a command (``/<type>``) and return the parsed JSON response."""
    async with httpx.AsyncClient(base_url=url) as client:
        resp = await client.post("/{}".format(message["type"]), json=message, timeout=timeout)
        return resp.json()


# =========================================================================
# Health
# =========================================================================


class TestHealthEndpoint:
    async def test_health_response_shape(self, bridge_server):
        url, _ = bridge_server
        async with httpx.AsyncClient(base_url=url) as client:
            resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["runtime_mode"] == "test"
        assert body["version"]


# =========================================================================
# Execute code
# =========================================================================


class TestExecuteCodeProtocol:
    async def test_success(self, bridge_server):
        url, _ = bridge_server
        resp = await _send_recv(url, {"type": "execute_code", "request_id": "e1", "code": "print('hello')"})

        assert resp["type"] == "execute_code_result"
        assert resp["request_id"] == "e1"
        assert resp["ok"] is True
        assert "status" not in resp
        assert "hello" in resp["data"]["output"]

    async def test_syntax_error(self, bridge_server):
        url, _ = bridge_server
        resp = await _send_recv(url, {"type": "execute_code", "request_id": "e2", "code": "def ("})

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
        url, _ = bridge_server
        resp = await _send_recv(url, {"type": "execute_code", "request_id": "e4", "code": "1 + 2"})

        assert resp["ok"] is True
        assert resp["data"]["result"] == 3


# =========================================================================
# Task operations
# =========================================================================


class TestTaskProtocol:
    async def test_check_nonexistent_task(self, bridge_server):
        url, _ = bridge_server
        resp = await _send_recv(
            url,
            {
                "type": "check_task_status",
                "request_id": "t1",
                "task_id": "nonexistent",
            },
        )
        assert resp["ok"] is False
        assert resp["error"]["code"] == "not_found"

    async def test_list_tasks_empty(self, bridge_server):
        url, _ = bridge_server
        resp = await _send_recv(url, {"type": "list_tasks", "request_id": "l1"})
        assert resp["ok"] is True
        assert isinstance(resp["data"], list)

    async def test_execute_task_routes(self, bridge_server):
        # Missing script_path proves routing without running a script.
        url, _ = bridge_server
        resp = await _send_recv(url, {"type": "execute_task", "request_id": "x1"})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "missing_field"

    async def test_yade_task_legacy_alias_routes(self, bridge_server):
        url, _ = bridge_server
        resp = await _send_recv(url, {"type": "yade_task", "request_id": "x2"})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "missing_field"

    async def test_interrupt_nonexistent_task(self, bridge_server):
        url, _ = bridge_server
        resp = await _send_recv(
            url,
            {
                "type": "interrupt_task",
                "request_id": "i1",
                "task_id": "nonexistent",
            },
        )
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
    """End-to-end tests for bridge-side pagination of task output.

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

        resp = await _send_recv(
            url,
            {
                "type": "check_task_status",
                "request_id": "p1",
                "task_id": "tail1",
            },
        )

        data = resp["data"]
        assert data["status"] == "completed"
        assert data["pagination"]["total_lines"] == 200
        # Default limit=64 → last 64 lines (137-200): older lines exist, none newer.
        assert data["pagination"]["line_range"] == "137-200"
        # Default limit=64, so we should see the last 64 lines.
        assert "line 199" in data["output"]
        assert "line 136" in data["output"]
        assert "line 135" not in data["output"]

    async def test_skip_newest_and_limit(self, bridge_server_with_tasks):
        url, task_manager, tmp_path = bridge_server_with_tasks
        self._inject_task(task_manager, tmp_path, "skip1", [f"line {i}" for i in range(100)])

        resp = await _send_recv(
            url,
            {
                "type": "check_task_status",
                "request_id": "p2",
                "task_id": "skip1",
                "skip_newest": 10,
                "limit": 5,
            },
        )

        data = resp["data"]
        assert data["pagination"]["total_lines"] == 100
        # Mid-log window (86-90 of 100): older and newer lines both exist.
        assert data["pagination"]["line_range"] == "86-90"
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

        resp = await _send_recv(
            url,
            {
                "type": "check_task_status",
                "request_id": "p3",
                "task_id": "filter1",
                "filter_text": "error",
                "limit": 10,
            },
        )

        data = resp["data"]
        assert data["pagination"]["total_lines"] == 3
        assert "error at step 5" in data["output"]
        assert "error at step 150" in data["output"]
        assert "error at step 295" in data["output"]
        assert "ok " not in data["output"]

    async def test_empty_log_returns_empty_pagination(self, bridge_server_with_tasks):
        url, task_manager, tmp_path = bridge_server_with_tasks
        self._inject_task(task_manager, tmp_path, "empty1", [])

        resp = await _send_recv(
            url,
            {
                "type": "check_task_status",
                "request_id": "p4",
                "task_id": "empty1",
            },
        )

        data = resp["data"]
        assert data["pagination"]["total_lines"] == 0
        assert data["pagination"]["line_range"] == "0-0"


# =========================================================================
# Error handling
# =========================================================================


class TestErrorHandling:
    async def test_invalid_json(self, bridge_server):
        url, _ = bridge_server
        # POST a malformed body to a valid command path -> 400 invalid_json.
        async with httpx.AsyncClient(base_url=url) as client:
            resp = await client.post("/list_tasks", content=b"not json{{{")
        assert resp.status_code == 400
        body = resp.json()
        assert body["type"] == "error"
        assert body["error"]["code"] == "invalid_json"
        assert "Invalid JSON" in body["error"]["message"]

    async def test_unknown_command_returns_404(self, bridge_server):
        url, _ = bridge_server
        async with httpx.AsyncClient(base_url=url) as client:
            resp = await client.post("/unknown_type", json={"type": "unknown_type", "request_id": "u1"})
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "unknown_command"

    async def test_unknown_command_lists_available_commands(self, bridge_server):
        # Self-correction loop: a caller that guesses a wrong command gets
        # the canonical command list back — legacy aliases excluded.
        url, _ = bridge_server
        async with httpx.AsyncClient(base_url=url) as client:
            resp = await client.post("/run_script", json={"type": "run_script", "request_id": "u2"})
        available = resp.json()["error"]["details"]["available_commands"]
        assert "execute_code" in available
        assert "execute_task" in available
        assert "yade_task" not in available


# =========================================================================
# Connection management
# =========================================================================


class TestConnectionManagement:
    async def test_multiple_requests_on_same_connection(self, bridge_server):
        url, _ = bridge_server
        async with httpx.AsyncClient(base_url=url) as client:
            for i in range(3):
                resp = await client.post("/list_tasks", json={"type": "list_tasks", "request_id": f"m{i}"})
                body = resp.json()
                assert body["request_id"] == f"m{i}"
                assert body["type"] == "result"

    async def test_concurrent_connections(self, bridge_server):
        url, _ = bridge_server

        async def list_tasks(client_id):
            async with httpx.AsyncClient(base_url=url) as client:
                resp = await client.post("/list_tasks", json={"type": "list_tasks", "request_id": client_id})
                return resp.json()

        results = await asyncio.gather(list_tasks("c1"), list_tasks("c2"), list_tasks("c3"))
        ids = {r["request_id"] for r in results}
        assert ids == {"c1", "c2", "c3"}


# =========================================================================
# execute_code timeout termination (end-to-end)
# =========================================================================


class TestExecuteCodeTimeoutTermination:
    async def test_tight_loop_times_out_and_terminates_cleanly(self, bridge_server_with_pump):
        """Pure-Python infinite loop hits short timeout → SetAsyncExc
        aborts it → status="terminated"."""
        url, _ = bridge_server_with_pump
        resp = await _send_recv(
            url,
            {
                "type": "execute_code",
                "request_id": "term-tight",
                "code": "while True:\n    pass",
                "timeout_ms": 500,
            },
        )

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

        first = await _send_recv(
            url,
            {
                "type": "execute_code",
                "request_id": "pre",
                "code": "while True:\n    pass",
                "timeout_ms": 500,
            },
        )
        assert first["error"]["code"] == "terminated"

        # Now a quick call on the SAME pump: must succeed fast.
        t0 = time.time()
        second = await _send_recv(
            url,
            {
                "type": "execute_code",
                "request_id": "post",
                "code": "1 + 1",
                "timeout_ms": 1000,
            },
        )
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
        resp = await _send_recv(
            url,
            {
                "type": "execute_code",
                "request_id": "swallow",
                "code": code,
                "timeout_ms": 300,
            },
        )

        # Either "terminated" (if injection happens OUTSIDE the try,
        # e.g., in the while-condition evaluation) or "timeout"
        # (stuck_in_c — pump didn't resolve within grace). Both are
        # acceptable — the point is that the pump does eventually
        # recover. Accept either outcome.
        assert resp["ok"] is False
        assert resp["error"]["code"] in ("terminated", "timeout")
        # In either case, confirm pump recovers with a follow-up call.
        # Wait for the bridge's 1.5s self-termination in the stuck case.
        await asyncio.sleep(2.0)
        follow = await _send_recv(
            url,
            {
                "type": "execute_code",
                "request_id": "after",
                "code": "42",
                "timeout_ms": 2000,
            },
        )
        assert follow["ok"] is True

    async def test_execute_code_does_not_clobber_current_task_id(self, bridge_server_with_pump):
        """Regression: _execute_code must NOT set_current_task(request_id).

        If it did, a subsequent REPL timeout's request_interrupt() would
        set a flag that PyRunner's is_current_interrupt_requested() reads
        via _current_task_id → O.pause() fires → _hooked_run raises
        InterruptedError → the enclosing script task gets spuriously
        marked ``interrupted``. The fix: leave _current_task_id alone.
        """
        url, _ = bridge_server_with_pump
        from yade_mcp_bridge.runtime.signals import clear_current_task, peek_current_task, set_current_task

        # Simulate a running task by setting the sentinel outer task.
        clear_current_task()
        set_current_task("outer-task")
        try:
            # Normal, successful execute_code.
            ok = await _send_recv(
                url,
                {
                    "type": "execute_code",
                    "request_id": "nested-ok",
                    "code": "1 + 1",
                    "timeout_ms": 2000,
                },
            )
            assert ok["ok"] is True
            # After execute_code completes, the outer task must still
            # be the current one — not None, not request_id.
            assert peek_current_task() == "outer-task"

            # Timed-out execute_code: the termination path calls
            # request_interrupt(request_id) on the REPL's own id.
            # That flag must not leak into any PyRunner tick reading
            # _current_task_id.
            terminated = await _send_recv(
                url,
                {
                    "type": "execute_code",
                    "request_id": "nested-timeout",
                    "code": "while True:\n    pass",
                    "timeout_ms": 500,
                },
            )
            assert terminated["error"]["code"] == "terminated"
            assert peek_current_task() == "outer-task"
        finally:
            clear_current_task()
