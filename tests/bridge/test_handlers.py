"""Tests for bridge message handlers (ping, tasks, interrupt)."""

from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch

from yade_mcp_bridge.handlers.context import ServerContext
from yade_mcp_bridge.handlers.execute_code import (
    _terminate_stuck_execution,
    _timeout_response,
)
from yade_mcp_bridge.handlers.tasks import (
    handle_check_task_status,
    handle_interrupt_task,
    handle_list_tasks,
    handle_yade_task,
)
from yade_mcp_bridge.handlers.utilities import handle_ping
from yade_mcp_bridge.signals import (
    _exec_thread_ids,
    _interrupt_requested,
    clear_current_task,
    register_exec_thread,
)


def _make_ctx(runtime_mode="console", tasks=None):
    """Create a ServerContext with mock dependencies."""
    task_manager = MagicMock()
    task_manager.tasks = tasks or {}
    script_runner = MagicMock()
    main_executor = MagicMock()
    return ServerContext(
        task_manager=task_manager,
        script_runner=script_runner,
        main_executor=main_executor,
        runtime_mode=runtime_mode,
    )


# =========================================================================
# Ping
# =========================================================================


class TestHandlePing:
    async def test_ping_returns_pong(self):
        ctx = _make_ctx(runtime_mode="gui")
        resp = await handle_ping(ctx, {"request_id": "r1"})
        assert resp["status"] == "success"
        assert resp["message"] == "pong"
        assert resp["data"]["runtime_mode"] == "gui"
        assert resp["request_id"] == "r1"

    async def test_ping_default_request_id(self):
        ctx = _make_ctx()
        resp = await handle_ping(ctx, {})
        assert resp["request_id"] == "unknown"


# =========================================================================
# Check task status
# =========================================================================


class TestHandleCheckTaskStatus:
    async def test_missing_task_id(self):
        ctx = _make_ctx()
        resp = await handle_check_task_status(ctx, {"request_id": "r1"})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "missing_field"
        assert "task_id required" in resp["error"]["message"]

    async def test_delegates_to_task_manager(self):
        ctx = _make_ctx()
        ctx.task_manager.get_task_status.return_value = {
            "ok": True,
            "data": {"status": "completed"},
        }
        resp = await handle_check_task_status(ctx, {"request_id": "r1", "task_id": "t1"})
        ctx.task_manager.get_task_status.assert_called_once_with(
            "t1", skip_newest=0, limit=64, filter_text=None,
        )
        assert resp["ok"] is True
        assert resp["data"]["status"] == "completed"
        assert resp["request_id"] == "r1"

    async def test_forwards_pagination_params(self):
        ctx = _make_ctx()
        ctx.task_manager.get_task_status.return_value = {"ok": True, "data": {"status": "running"}}
        await handle_check_task_status(ctx, {
            "request_id": "r1",
            "task_id": "t1",
            "skip_newest": 10,
            "limit": 32,
            "filter_text": "error",
        })
        ctx.task_manager.get_task_status.assert_called_once_with(
            "t1", skip_newest=10, limit=32, filter_text="error",
        )


# =========================================================================
# List tasks
# =========================================================================


class TestHandleListTasks:
    async def test_delegates_to_task_manager(self):
        ctx = _make_ctx()
        ctx.task_manager.list_all_tasks.return_value = {
            "ok": True,
            "data": [],
        }
        resp = await handle_list_tasks(ctx, {"request_id": "r1"})
        ctx.task_manager.list_all_tasks.assert_called_once_with(offset=0, limit=None)
        assert resp["ok"] is True

    async def test_passes_pagination(self):
        ctx = _make_ctx()
        ctx.task_manager.list_all_tasks.return_value = {"ok": True, "data": []}
        await handle_list_tasks(ctx, {"request_id": "r1", "offset": 5, "limit": 10})
        ctx.task_manager.list_all_tasks.assert_called_once_with(offset=5, limit=10)


# =========================================================================
# Interrupt task
# =========================================================================


class TestHandleInterruptTask:
    async def test_missing_task_id(self):
        ctx = _make_ctx()
        resp = await handle_interrupt_task(ctx, {"request_id": "r1"})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "missing_field"
        assert "task_id required" in resp["error"]["message"]

    async def test_task_not_found(self):
        ctx = _make_ctx(tasks={})
        resp = await handle_interrupt_task(ctx, {"request_id": "r1", "task_id": "nope"})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "not_found"
        assert "not found" in resp["error"]["message"].lower()

    async def test_task_already_completed(self):
        task = MagicMock()
        task.status = "completed"
        ctx = _make_ctx(tasks={"t1": task})
        resp = await handle_interrupt_task(ctx, {"request_id": "r1", "task_id": "t1"})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "already_terminal"
        assert "terminal state" in resp["error"]["message"].lower()

    async def test_interrupt_running_task(self):
        from yade_mcp_bridge.signals import clear_interrupt, is_interrupt_requested

        task = MagicMock()
        task.status = "running"
        ctx = _make_ctx(tasks={"t1": task})
        clear_interrupt("t1")
        resp = await handle_interrupt_task(ctx, {"request_id": "r1", "task_id": "t1"})
        try:
            assert resp["ok"] is True
            assert resp["data"]["interrupt_requested"] is True
            assert is_interrupt_requested("t1") is True
            # No thread registered for "t1" → async-exc path must skip,
            # flag-only method reported.
            assert resp["data"]["method"] == "flag_only"
            # Continuation hint must be surfaced so the agent knows the
            # YADE namespace is preserved and how to pick up work.
            assert resp["data"]["namespace_preserved"] is True
            assert "yade_execute_code" in resp["data"]["continuation_hint"]
        finally:
            clear_interrupt("t1")

    async def test_interrupt_running_task_with_registered_thread_fires_async_exc(self):
        """When ScriptRunner has registered a live script thread for
        the task, the handler atomically unregisters and injects
        TaskInterrupt. The test stands up a real thread to validate
        the end-to-end SetAsyncExc injection."""
        import threading

        from yade_mcp_bridge.execution.errors import TaskInterrupt
        from yade_mcp_bridge.signals import (
            clear_interrupt,
            get_exec_thread,
            register_exec_thread,
            unregister_exec_thread,
        )

        task = MagicMock()
        task.status = "running"
        ctx = _make_ctx(tasks={"t2": task})
        clear_interrupt("t2")
        unregister_exec_thread("t2")

        got_exception: list[BaseException] = []
        started: list[bool] = []

        def _loop():
            started.append(True)
            try:
                while True:
                    pass  # pure-Python bytecode edge every instruction
            except BaseException as e:
                got_exception.append(e)

        t = threading.Thread(target=_loop, name="script-t2", daemon=True)
        t.start()
        # Busy-spin until the thread signals it's in the loop.
        for _ in range(100):
            if started:
                break
            import time as _time

            _time.sleep(0.01)
        register_exec_thread("t2", t.ident)

        try:
            resp = await handle_interrupt_task(ctx, {"request_id": "r1", "task_id": "t2"})
            t.join(timeout=2.0)

            assert resp["ok"] is True
            assert resp["data"]["method"] == "flag_and_async_exc"
            assert not t.is_alive()
            assert len(got_exception) == 1
            assert isinstance(got_exception[0], TaskInterrupt)
            # Atomicity check: registry must be cleared so a second
            # interrupt call can't re-inject during cleanup.
            assert get_exec_thread("t2") is None
        finally:
            t.join(timeout=1.0)
            clear_interrupt("t2")
            unregister_exec_thread("t2")

    async def test_second_interrupt_is_noop_on_async_exc_path(self):
        """Re-entrancy guard: once handler unregisters the thread on
        first interrupt, a second handler call must NOT re-inject
        (which would interrupt the script's except-block cleanup)."""
        from yade_mcp_bridge.signals import (
            clear_interrupt,
            unregister_exec_thread,
        )

        task = MagicMock()
        task.status = "running"
        ctx = _make_ctx(tasks={"t3": task})
        clear_interrupt("t3")

        # Simulate: script thread already exited its body, registry cleared
        # by prior interrupt. A second call here should be flag-only.
        unregister_exec_thread("t3")
        resp = await handle_interrupt_task(ctx, {"request_id": "r1", "task_id": "t3"})
        try:
            assert resp["ok"] is True
            assert resp["data"]["method"] == "flag_only"
            assert resp["data"].get("async_exc_skipped_reason") is None
        finally:
            clear_interrupt("t3")

    async def test_dummy_thread_registration_falls_back_to_flag_only(self):
        """If something weird registers a Dummy-N tid (shouldn't happen
        for script tasks, but defense in depth), the handler must
        refuse async-exc and report a reason."""
        from unittest.mock import patch

        from yade_mcp_bridge.signals import clear_interrupt, register_exec_thread, unregister_exec_thread

        task = MagicMock()
        task.status = "running"
        ctx = _make_ctx(tasks={"t4": task})
        clear_interrupt("t4")

        class _FakeDummy:
            ident = 98765
            name = "Dummy-99"

            def is_alive(self):
                return True

        register_exec_thread("t4", 98765)
        with patch(
            "yade_mcp_bridge.execution.termination._find_thread",
            return_value=_FakeDummy(),
        ):
            resp = await handle_interrupt_task(ctx, {"request_id": "r1", "task_id": "t4"})
        try:
            assert resp["ok"] is True
            assert resp["data"]["method"] == "flag_only"
            assert resp["data"]["async_exc_skipped_reason"] == "nested_boost_python_callback"
        finally:
            clear_interrupt("t4")
            unregister_exec_thread("t4")

    async def test_interrupt_pending_task(self):
        from yade_mcp_bridge.signals import clear_interrupt

        task = MagicMock()
        task.status = "pending"
        ctx = _make_ctx(tasks={"t1": task})
        clear_interrupt("t1")
        resp = await handle_interrupt_task(ctx, {"request_id": "r1", "task_id": "t1"})
        try:
            assert resp["ok"] is True
        finally:
            clear_interrupt("t1")


# =========================================================================
# Yade task
# =========================================================================


class TestHandleYadeTask:
    async def test_missing_script_path(self):
        ctx = _make_ctx()
        resp = await handle_yade_task(ctx, {"request_id": "r1", "task_id": "t1"})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "missing_field"
        assert "script_path required" in resp["error"]["message"]

    async def test_missing_task_id(self):
        ctx = _make_ctx()
        resp = await handle_yade_task(ctx, {"request_id": "r1", "script_path": "/s.py"})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "missing_field"
        assert "task_id required" in resp["error"]["message"]

    async def test_delegates_to_script_runner(self):
        ctx = _make_ctx()
        ctx.script_runner.run = AsyncMock(
            return_value={"ok": True, "data": {"status": "pending"}}
        )
        resp = await handle_yade_task(ctx, {
            "request_id": "r1",
            "script_path": "/tmp/test.py",
            "task_id": "t1",
            "description": "my task",
        })
        ctx.script_runner.run.assert_called_once()


# =========================================================================
# execute_code timeout termination
# =========================================================================


class TestTerminateStuckExecution:
    def setup_method(self):
        _exec_thread_ids.clear()
        _interrupt_requested.clear()
        clear_current_task()

    async def test_no_thread_registered_resolved_future(self):
        """Registry empty + future already done → 'self' path, resolved."""
        future = Future()
        future.set_result({"status": "success", "output": "hi"})

        result = await _terminate_stuck_execution("req-1", future)

        assert result["resolved"] is True
        assert result["method"] == "self"
        assert result["result"]["status"] == "success"
        # request_interrupt still fires, even on the self path
        assert _interrupt_requested.get("req-1") is True

    async def test_no_thread_registered_pending_future(self):
        """Registry empty + future pending → 'self', not resolved.
        Defensive: shouldn't happen in practice (finally would have
        resolved the future before clearing the registry)."""
        future = Future()
        result = await _terminate_stuck_execution("req-1", future)
        assert result["resolved"] is False
        assert result["method"] == "self"

    async def test_flag_only_when_thread_is_dummy(self):
        """Dummy-N thread (nested via PyRunner) → flag-only fallback."""
        future = Future()

        class _FakeDummy:
            ident = 99999
            name = "Dummy-3"

            def is_alive(self):
                return True

        register_exec_thread("req-2", 99999)

        with patch(
            "yade_mcp_bridge.execution.termination._find_thread",
            return_value=_FakeDummy(),
        ):
            result = await _terminate_stuck_execution("req-2", future)

        assert result["resolved"] is False
        assert result["method"] == "flag_only"
        assert result["reason"] == "nested_boost_python_callback"
        assert _interrupt_requested.get("req-2") is True

    async def test_async_exc_resolves_future_in_grace_period(self):
        """SetAsyncExc succeeds → future resolves → method=async_exc."""
        future = Future()

        # Construct a fake thread that passes all safety guards — we
        # don't need a real thread here since we patch
        # fire_async_exception.
        class _FakeSafe:
            def __init__(self):
                self.ident = 77777
                self.name = "mcp-task-pump"

            def is_alive(self):
                return True

        register_exec_thread("req-3", 77777)
        # Resolve future BEFORE calling — grace wait_for will see it immediately
        future.set_result({"status": "terminated", "output": "partial"})

        with patch(
            "yade_mcp_bridge.execution.termination._find_thread",
            return_value=_FakeSafe(),
        ), patch(
            "yade_mcp_bridge.handlers.execute_code.fire_async_exception",
            return_value=1,
        ):
            result = await _terminate_stuck_execution("req-3", future)

        assert result["resolved"] is True
        assert result["method"] == "async_exc"
        assert result["result"]["output"] == "partial"

    async def test_stuck_in_c_when_future_never_resolves(self):
        """SetAsyncExc called but future never resolves → stuck_in_c."""
        future = Future()  # never resolved

        class _FakeSafe:
            ident = 88888
            name = "mcp-task-pump"

            def is_alive(self):
                return True

        register_exec_thread("req-4", 88888)

        with patch(
            "yade_mcp_bridge.execution.termination._find_thread",
            return_value=_FakeSafe(),
        ), patch(
            "yade_mcp_bridge.handlers.execute_code.fire_async_exception",
            return_value=1,
        ), patch(
            "yade_mcp_bridge.handlers.execute_code._TERMINATION_GRACE_S",
            0.05,  # short grace to keep test fast
        ):
            result = await _terminate_stuck_execution("req-4", future)

        assert result["resolved"] is False
        assert result["method"] == "stuck_in_c"


class TestTimeoutResponse:
    def test_resolved_returns_terminated(self):
        termination = {
            "resolved": True,
            "method": "async_exc",
            "result": {"status": "terminated", "output": "hello"},
        }
        resp = _timeout_response("req-1", 3000, termination)
        assert resp["ok"] is False
        assert "status" not in resp
        assert resp["type"] == "execute_code_result"
        assert resp["request_id"] == "req-1"
        assert "aborted" in resp["error"]["message"].lower()
        assert resp["data"]["output"] == "hello"
        assert resp["error"]["code"] == "terminated"
        assert resp["error"]["details"]["method"] == "async_exc"

    def test_flag_only_returns_timeout_with_reason(self):
        termination = {
            "resolved": False,
            "method": "flag_only",
            "reason": "nested_boost_python_callback",
            "result": None,
        }
        resp = _timeout_response("req-2", 3000, termination)
        assert resp["ok"] is False
        assert resp["error"]["code"] == "timeout"
        assert "nested_boost_python_callback" in resp["error"]["message"]
        assert resp["error"]["details"]["method"] == "flag_only"
        assert resp["error"]["details"]["reason"] == "nested_boost_python_callback"

    def test_stuck_in_c_returns_timeout(self):
        termination = {
            "resolved": False,
            "method": "stuck_in_c",
            "result": None,
        }
        resp = _timeout_response("req-3", 3000, termination)
        assert resp["ok"] is False
        assert resp["error"]["code"] == "timeout"
        assert "C extension" in resp["error"]["message"]
        assert resp["data"]["output"] == ""
