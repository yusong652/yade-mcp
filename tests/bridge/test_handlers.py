"""Tests for bridge message handlers (tasks, interrupt)."""

from concurrent.futures import Future
from unittest.mock import MagicMock, patch

from yade_mcp_bridge.execution.code_runner import (
    _terminateStuckExecution,
    _timeoutResponse,
)
from yade_mcp_bridge.handlers.context import ServerContext
from yade_mcp_bridge.handlers.tasks import (
    handleCheckTaskStatus,
    handleExecuteTask,
    handleInterruptTask,
    handleListTasks,
)
from yade_mcp_bridge.runtime.signals import (
    _execThreadIds,
    _interruptRequested,
    clearCurrentTask,
    getCurrentTask,
    registerExecThread,
    setCurrentTask,
)


def _make_ctx(runtimeMode="console", tasks=None):
    """Create a ServerContext with mock dependencies."""
    taskManager = MagicMock()
    taskManager.tasks = tasks or {}
    scriptRunner = MagicMock()
    codeRunner = MagicMock()
    executor = MagicMock()
    return ServerContext(
        taskManager=taskManager,
        scriptRunner=scriptRunner,
        codeRunner=codeRunner,
        executor=executor,
        runtimeMode=runtimeMode,
    )


# =========================================================================
# Check task status
# =========================================================================


class TestHandleCheckTaskStatus:
    def test_missing_task_id(self):
        ctx = _make_ctx()
        resp = handleCheckTaskStatus(ctx, {"request_id": "r1"})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "missing_field"
        assert "task_id required" in resp["error"]["message"]

    def test_delegates_to_task_manager(self):
        ctx = _make_ctx()
        ctx.taskManager.getTaskStatus.return_value = {
            "ok": True,
            "data": {"status": "completed"},
        }
        resp = handleCheckTaskStatus(ctx, {"request_id": "r1", "task_id": "t1"})
        ctx.taskManager.getTaskStatus.assert_called_once_with(
            "t1",
            skipNewest=0,
            limit=64,
            filterText=None,
        )
        assert resp["ok"] is True
        assert resp["data"]["status"] == "completed"
        assert resp["request_id"] == "r1"

    def test_forwards_pagination_params(self):
        ctx = _make_ctx()
        ctx.taskManager.getTaskStatus.return_value = {"ok": True, "data": {"status": "running"}}
        handleCheckTaskStatus(
            ctx,
            {
                "request_id": "r1",
                "task_id": "t1",
                "skip_newest": 10,
                "limit": 32,
                "filter_text": "error",
            },
        )
        ctx.taskManager.getTaskStatus.assert_called_once_with(
            "t1",
            skipNewest=10,
            limit=32,
            filterText="error",
        )


# =========================================================================
# List tasks
# =========================================================================


class TestHandleListTasks:
    def test_delegates_to_task_manager(self):
        ctx = _make_ctx()
        ctx.taskManager.listAllTasks.return_value = {
            "ok": True,
            "data": [],
        }
        resp = handleListTasks(ctx, {"request_id": "r1"})
        ctx.taskManager.listAllTasks.assert_called_once_with(offset=0, limit=64)
        assert resp["ok"] is True

    def test_explicit_null_limit_uses_default(self):
        ctx = _make_ctx()
        ctx.taskManager.listAllTasks.return_value = {"ok": True, "data": []}
        handleListTasks(ctx, {"request_id": "r1", "limit": None})
        ctx.taskManager.listAllTasks.assert_called_once_with(offset=0, limit=64)

    def test_passes_pagination(self):
        ctx = _make_ctx()
        ctx.taskManager.listAllTasks.return_value = {"ok": True, "data": []}
        handleListTasks(ctx, {"request_id": "r1", "offset": 5, "limit": 10})
        ctx.taskManager.listAllTasks.assert_called_once_with(offset=5, limit=10)


# =========================================================================
# Interrupt task
# =========================================================================


class TestHandleInterruptTask:
    def test_missing_task_id(self):
        ctx = _make_ctx()
        resp = handleInterruptTask(ctx, {"request_id": "r1"})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "missing_field"
        assert "task_id required" in resp["error"]["message"]

    def test_task_not_found(self):
        ctx = _make_ctx(tasks={})
        resp = handleInterruptTask(ctx, {"request_id": "r1", "task_id": "nope"})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "not_found"
        assert "not found" in resp["error"]["message"].lower()

    def test_task_already_completed(self):
        task = MagicMock()
        task.status = "completed"
        ctx = _make_ctx(tasks={"t1": task})
        resp = handleInterruptTask(ctx, {"request_id": "r1", "task_id": "t1"})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "already_terminal"
        assert "terminal state" in resp["error"]["message"].lower()

    def test_interrupt_running_task(self):
        from yade_mcp_bridge.runtime.signals import clearInterrupt, isTaskInterruptRequested

        task = MagicMock()
        task.status = "running"
        ctx = _make_ctx(tasks={"t1": task})
        clearInterrupt("t1")
        resp = handleInterruptTask(ctx, {"request_id": "r1", "task_id": "t1"})
        try:
            assert resp["ok"] is True
            assert resp["data"]["interrupt_requested"] is True
            assert isTaskInterruptRequested("t1") is True
            # No thread registered for "t1" → async-exc path must skip,
            # flag-only method reported.
            assert resp["data"]["method"] == "flag_only"
        finally:
            clearInterrupt("t1")

    def test_interrupt_running_task_with_registered_thread_fires_async_exc(self):
        """When ScriptRunner has registered a live script thread for
        the task, the handler atomically unregisters and injects
        AsyncAbort. The test stands up a real thread to validate
        the end-to-end SetAsyncExc injection."""
        import threading

        from yade_mcp_bridge.execution.termination import AsyncAbort
        from yade_mcp_bridge.runtime.signals import (
            clearInterrupt,
            getExecThread,
            registerExecThread,
            unregisterExecThread,
        )

        task = MagicMock()
        task.status = "running"
        ctx = _make_ctx(tasks={"t2": task})
        clearInterrupt("t2")
        unregisterExecThread("t2")

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
        registerExecThread("t2", t.ident)

        try:
            resp = handleInterruptTask(ctx, {"request_id": "r1", "task_id": "t2"})
            t.join(timeout=2.0)

            assert resp["ok"] is True
            assert resp["data"]["method"] == "flag_and_async_exc"
            assert not t.is_alive()
            assert len(got_exception) == 1
            assert isinstance(got_exception[0], AsyncAbort)
            # Atomicity check: registry must be cleared so a second
            # interrupt call can't re-inject during cleanup.
            assert getExecThread("t2") is None
        finally:
            t.join(timeout=1.0)
            clearInterrupt("t2")
            unregisterExecThread("t2")

    def test_second_interrupt_is_noop_on_async_exc_path(self):
        """Re-entrancy guard: once handler unregisters the thread on
        first interrupt, a second handler call must NOT re-inject
        (which would interrupt the script's except-block cleanup)."""
        from yade_mcp_bridge.runtime.signals import (
            clearInterrupt,
            unregisterExecThread,
        )

        task = MagicMock()
        task.status = "running"
        ctx = _make_ctx(tasks={"t3": task})
        clearInterrupt("t3")

        # Simulate: script thread already exited its body, registry cleared
        # by prior interrupt. A second call here should be flag-only.
        unregisterExecThread("t3")
        resp = handleInterruptTask(ctx, {"request_id": "r1", "task_id": "t3"})
        try:
            assert resp["ok"] is True
            assert resp["data"]["method"] == "flag_only"
        finally:
            clearInterrupt("t3")

    def test_interrupt_pending_task(self):
        from yade_mcp_bridge.runtime.signals import clearInterrupt

        task = MagicMock()
        task.status = "pending"
        ctx = _make_ctx(tasks={"t1": task})
        clearInterrupt("t1")
        resp = handleInterruptTask(ctx, {"request_id": "r1", "task_id": "t1"})
        try:
            assert resp["ok"] is True
        finally:
            clearInterrupt("t1")


# =========================================================================
# Execute task
# =========================================================================


class TestHandleExecuteTask:
    def test_missing_script_path(self):
        ctx = _make_ctx()
        resp = handleExecuteTask(ctx, {"request_id": "r1", "task_id": "t1"})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "missing_field"
        assert "script_path required" in resp["error"]["message"]

    def test_delegates_to_script_runner(self):
        ctx = _make_ctx()
        ctx.scriptRunner.run = MagicMock(return_value={"ok": True, "data": {"status": "pending"}})
        handleExecuteTask(
            ctx,
            {
                "request_id": "r1",
                "script_path": "/tmp/test.py",
                "description": "my task",
            },
        )
        ctx.scriptRunner.run.assert_called_once_with("/tmp/test.py", "my task")

    def test_run_assigns_task_id(self, tmp_path, monkeypatch):
        """ScriptRunner.run assigns the task_id and returns it in the data."""
        import os

        from yade_mcp_bridge.execution.script_runner import ScriptRunner
        from yade_mcp_bridge.paths import LOGS_DIR

        monkeypatch.chdir(tmp_path)
        os.makedirs(LOGS_DIR)
        script = tmp_path / "s.py"
        script.write_text("x = 1\n")
        taskManager = MagicMock()
        taskManager.tasks = {}

        result = ScriptRunner(taskManager=taskManager).run(str(script), "desc")

        assert result["ok"] is True
        assert result["data"]["task_id"]


# =========================================================================
# execute_code timeout termination
# =========================================================================


class TestTerminateStuckExecution:
    def setup_method(self):
        _execThreadIds.clear()
        _interruptRequested.clear()
        clearCurrentTask()

    def test_no_thread_registered_resolved_future(self):
        """Registry empty + future already done → 'finished' path, resolved."""
        future = Future()
        future.set_result({"status": "success", "output": "hi"})

        result = _terminateStuckExecution("req-1", future)

        assert result["result"] is not None
        assert result["method"] == "finished"
        assert result["result"]["status"] == "success"
        # requestInterrupt still fires, even on the finished path
        assert _interruptRequested.get("req-1") is True

    def test_no_thread_registered_pending_future(self):
        """Registry empty + future pending → 'unsettled'.
        Defensive: the ultra-narrow window where the registry is cleared but
        the executor hasn't set the future's result yet."""
        future = Future()
        result = _terminateStuckExecution("req-1", future)
        assert result["result"] is None
        assert result["method"] == "unsettled"

    def test_async_exc_resolves_future_in_grace_period(self):
        """SetAsyncExc succeeds → future resolves → method=async_exc."""
        future = Future()

        registerExecThread("req-3", 77777)
        # Resolve future BEFORE calling — grace wait_for will see it immediately
        future.set_result({"status": "terminated", "output": "partial"})

        with patch(
            "yade_mcp_bridge.execution.code_runner.injectAsyncException",
            return_value=1,
        ):
            result = _terminateStuckExecution("req-3", future)

        assert result["result"] is not None
        assert result["method"] == "async_exc"
        assert result["result"]["output"] == "partial"

    def test_stuck_in_c_when_future_never_resolves(self):
        """SetAsyncExc called but future never resolves → stuck_in_c."""
        future = Future()  # never resolved

        registerExecThread("req-4", 88888)

        with (
            patch(
                "yade_mcp_bridge.execution.code_runner.injectAsyncException",
                return_value=1,
            ),
            patch(
                "yade_mcp_bridge.execution.code_runner._TERMINATION_GRACE_S",
                0.05,  # short grace to keep test fast
            ),
        ):
            result = _terminateStuckExecution("req-4", future)

        assert result["result"] is None
        assert result["method"] == "stuck_in_c"

    # --- cycle-interrupt path (standalone O.run inside execute_code) ---

    def test_cycle_interrupt_when_sim_running_and_no_task(self):
        """No task owns the sim + O.running → arm the flag, future
        resolves with status=interrupted → method='cycle_interrupt'."""
        future = Future()
        future.set_result({"status": "interrupted", "output": "paused"})

        # No task owns the sim (setup cleared _current_task_id).
        with patch(
            "yade_mcp_bridge.execution.code_runner._simRunning",
            return_value=True,
        ):
            result = _terminateStuckExecution("cyc-1", future)

        assert result["result"] is not None
        assert result["method"] == "cycle_interrupt"
        assert result["result"]["output"] == "paused"
        # CAS-cleared on the way out; interrupt flag cleared too.
        assert getCurrentTask() is None
        assert _interruptRequested.get("cyc-1") is None

    def test_cycle_stuck_when_future_never_resolves(self):
        """Sim running, no task, but O.pause doesn't free O.run within
        grace → method='cycle_stuck', unresolved; state still cleaned."""
        future = Future()  # never resolved

        with (
            patch(
                "yade_mcp_bridge.execution.code_runner._simRunning",
                return_value=True,
            ),
            patch(
                "yade_mcp_bridge.execution.code_runner._CYCLE_INTERRUPT_GRACE_S",
                0.05,  # short grace to keep test fast
            ),
        ):
            result = _terminateStuckExecution("cyc-2", future)

        assert result["result"] is None
        assert result["method"] == "cycle_stuck"
        assert getCurrentTask() is None
        assert _interruptRequested.get("cyc-2") is None

    def test_cycle_gate_skipped_when_task_owns_sim(self):
        """A task owns the sim (peek != None) → cycle gate skipped, the
        task's _current_task_id is never touched, async/self path runs."""
        future = Future()
        future.set_result({"status": "success", "output": "hi"})
        setCurrentTask("owner-task")

        with patch(
            "yade_mcp_bridge.execution.code_runner._simRunning",
            return_value=True,
        ):
            result = _terminateStuckExecution("cyc-3", future)

        # Falls through to the non-cycle path; no exec thread registered
        # → 'finished'. The task's slot is untouched.
        assert result["method"] == "finished"
        assert getCurrentTask() == "owner-task"

    def test_cycle_gate_skipped_when_sim_not_running(self):
        """O not running (e.g. no YADE / pure-Python stuck) → cycle gate
        skipped, existing behavior preserved."""
        future = Future()
        future.set_result({"status": "terminated", "output": ""})

        # _simRunning defaults to False without YADE; assert explicitly.
        with patch(
            "yade_mcp_bridge.execution.code_runner._simRunning",
            return_value=False,
        ):
            result = _terminateStuckExecution("cyc-4", future)

        assert result["method"] == "finished"
        assert result["result"] is not None


class TestTimeoutResponse:
    def test_resolved_returns_terminated(self):
        termination = {
            "method": "async_exc",
            "result": {"status": "terminated", "output": "hello"},
        }
        resp = _timeoutResponse("req-1", 3000, termination)
        assert resp["ok"] is False
        assert "status" not in resp
        assert resp["type"] == "execute_code_result"
        assert resp["request_id"] == "req-1"
        assert "aborted" in resp["error"]["message"].lower()
        assert resp["data"]["output"] == "hello"
        assert resp["error"]["code"] == "terminated"
        assert resp["error"]["details"]["method"] == "async_exc"

    def test_stuck_in_c_returns_timeout(self):
        termination = {
            "method": "stuck_in_c",
            "result": None,
        }
        resp = _timeoutResponse("req-3", 3000, termination)
        assert resp["ok"] is False
        assert resp["error"]["code"] == "timeout"
        assert "C extension" in resp["error"]["message"]
        assert resp["data"]["output"] == ""

    def test_cycle_interrupt_returns_interrupted(self):
        termination = {
            "method": "cycle_interrupt",
            "result": {"status": "interrupted", "output": "iter=500"},
        }
        resp = _timeoutResponse("req-5", 3000, termination)
        assert resp["ok"] is False
        assert resp["error"]["code"] == "interrupted"
        # Bridge states the fact (paused at a boundary); no client-tool names —
        # the agent pull-back to yade_execute_task lives in the MCP layer.
        assert "yade_" not in resp["error"]["message"]
        assert "paused" in resp["error"]["message"]
        assert resp["data"]["output"] == "iter=500"
        assert resp["error"]["details"]["method"] == "cycle_interrupt"

    def test_cycle_stuck_returns_timeout(self):
        termination = {
            "method": "cycle_stuck",
            "result": None,
        }
        resp = _timeoutResponse("req-6", 3000, termination)
        assert resp["ok"] is False
        assert resp["error"]["code"] == "timeout"
        assert "O.run" in resp["error"]["message"]
        assert resp["error"]["details"]["method"] == "cycle_stuck"

    def test_unsettled_returns_timeout(self):
        # Defensive method: registry cleared but the future wasn't retrievable.
        termination = {
            "method": "unsettled",
            "result": None,
        }
        resp = _timeoutResponse("req-7", 3000, termination)
        assert resp["ok"] is False
        assert resp["error"]["code"] == "timeout"
        assert resp["error"]["details"]["method"] == "unsettled"

    def test_finished_returns_terminated(self):
        # The code raced the timeout and settled → terminated, with its stdout.
        termination = {
            "method": "finished",
            "result": {"status": "success", "output": "done"},
        }
        resp = _timeoutResponse("req-8", 3000, termination)
        assert resp["ok"] is False
        assert resp["error"]["code"] == "terminated"
        assert resp["data"]["output"] == "done"
        assert resp["error"]["details"]["method"] == "finished"


# =========================================================================
# execute_code hold limit
# =========================================================================


class TestExecuteHoldLimit:
    def test_execute_derives_hold_limit_from_timeout(self, monkeypatch):
        """_execute passes the request timeout plus the kill-chain graces as
        the hold limit, so the hold outlives the abort path and only expires
        for a snippet the abort could not reach."""
        import contextlib

        from yade_mcp_bridge.execution import code_runner

        captured = {}

        @contextlib.contextmanager
        def fake_hold(maxHoldS=None):
            captured["max_hold_s"] = maxHoldS
            yield True

        monkeypatch.setattr(code_runner, "holdSim", fake_hold)
        monkeypatch.setattr(code_runner, "_simRunning", lambda: True)

        result = code_runner._execute("req-hold", "1 + 1", timeoutMs=5000)

        assert result["status"] == "success"
        expected = 5.0 + code_runner._CYCLE_INTERRUPT_GRACE_S + code_runner._TERMINATION_GRACE_S + 1.0
        assert captured["max_hold_s"] == expected

    def test_execute_skips_hold_when_sim_idle(self, monkeypatch):
        """No task and no running sim → no hold at all."""
        from yade_mcp_bridge.execution import code_runner

        called = {"hold": False}

        def fake_hold(**kwargs):
            called["hold"] = True

        monkeypatch.setattr(code_runner, "holdSim", fake_hold)
        monkeypatch.setattr(code_runner, "_simRunning", lambda: False)

        result = code_runner._execute("req-nohold", "2 + 2", timeoutMs=5000)

        assert result["status"] == "success"
        assert called["hold"] is False
