"""Tests for bridge message handlers (ping, tasks, interrupt)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from yade_mcp_bridge.handlers.context import ServerContext
from yade_mcp_bridge.handlers.utilities import handle_ping
from yade_mcp_bridge.handlers.tasks import (
    handle_check_task_status,
    handle_interrupt_task,
    handle_list_tasks,
    handle_yade_task,
)
from yade_mcp_bridge.tasks.task import ScriptTask


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
        assert resp["status"] == "error"
        assert "task_id required" in resp["message"]

    async def test_delegates_to_task_manager(self):
        ctx = _make_ctx()
        ctx.task_manager.get_task_status.return_value = {
            "status": "success",
            "message": "Task completed",
        }
        resp = await handle_check_task_status(ctx, {"request_id": "r1", "task_id": "t1"})
        ctx.task_manager.get_task_status.assert_called_once_with(
            "t1", skip_newest=0, limit=64, filter_text=None,
        )
        assert resp["status"] == "success"
        assert resp["request_id"] == "r1"

    async def test_forwards_pagination_params(self):
        ctx = _make_ctx()
        ctx.task_manager.get_task_status.return_value = {"status": "success", "message": "ok"}
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
            "status": "success",
            "message": "Found 0 tracked task(s)",
            "data": [],
        }
        resp = await handle_list_tasks(ctx, {"request_id": "r1"})
        ctx.task_manager.list_all_tasks.assert_called_once_with(offset=0, limit=None)
        assert resp["status"] == "success"

    async def test_passes_pagination(self):
        ctx = _make_ctx()
        ctx.task_manager.list_all_tasks.return_value = {"status": "success", "data": []}
        await handle_list_tasks(ctx, {"request_id": "r1", "offset": 5, "limit": 10})
        ctx.task_manager.list_all_tasks.assert_called_once_with(offset=5, limit=10)


# =========================================================================
# Interrupt task
# =========================================================================


class TestHandleInterruptTask:
    async def test_missing_task_id(self):
        ctx = _make_ctx()
        resp = await handle_interrupt_task(ctx, {"request_id": "r1"})
        assert resp["status"] == "error"
        assert "task_id required" in resp["message"]

    async def test_task_not_found(self):
        ctx = _make_ctx(tasks={})
        resp = await handle_interrupt_task(ctx, {"request_id": "r1", "task_id": "nope"})
        assert resp["status"] == "error"
        assert "not found" in resp["message"].lower()

    async def test_task_already_completed(self):
        task = MagicMock()
        task.status = "completed"
        ctx = _make_ctx(tasks={"t1": task})
        resp = await handle_interrupt_task(ctx, {"request_id": "r1", "task_id": "t1"})
        assert resp["status"] == "error"
        assert "terminal state" in resp["message"].lower()

    async def test_interrupt_running_task(self):
        from yade_mcp_bridge.signals import clear_interrupt, is_interrupt_requested

        task = MagicMock()
        task.status = "running"
        ctx = _make_ctx(tasks={"t1": task})
        clear_interrupt("t1")
        resp = await handle_interrupt_task(ctx, {"request_id": "r1", "task_id": "t1"})
        try:
            assert resp["status"] == "success"
            assert resp["data"]["interrupt_requested"] is True
            assert is_interrupt_requested("t1") is True
        finally:
            clear_interrupt("t1")

    async def test_interrupt_pending_task(self):
        from yade_mcp_bridge.signals import clear_interrupt

        task = MagicMock()
        task.status = "pending"
        ctx = _make_ctx(tasks={"t1": task})
        clear_interrupt("t1")
        resp = await handle_interrupt_task(ctx, {"request_id": "r1", "task_id": "t1"})
        try:
            assert resp["status"] == "success"
        finally:
            clear_interrupt("t1")


# =========================================================================
# Yade task
# =========================================================================


class TestHandleYadeTask:
    async def test_missing_script_path(self):
        ctx = _make_ctx()
        resp = await handle_yade_task(ctx, {"request_id": "r1", "task_id": "t1"})
        assert resp["status"] == "error"
        assert "script_path required" in resp["message"]

    async def test_missing_task_id(self):
        ctx = _make_ctx()
        resp = await handle_yade_task(ctx, {"request_id": "r1", "script_path": "/s.py"})
        assert resp["status"] == "error"
        assert "task_id required" in resp["message"]

    async def test_delegates_to_script_runner(self):
        ctx = _make_ctx()
        ctx.script_runner.run = AsyncMock(
            return_value={"status": "pending", "message": "Script submitted"}
        )
        resp = await handle_yade_task(ctx, {
            "request_id": "r1",
            "script_path": "/tmp/test.py",
            "task_id": "t1",
            "description": "my task",
        })
        ctx.script_runner.run.assert_called_once()
