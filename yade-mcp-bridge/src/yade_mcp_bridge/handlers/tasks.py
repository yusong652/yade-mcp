# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Task-related message handlers for MCP bridge."""

import logging

from ..utils import errorResponse, okResponse
from .helpers import requireField

logger = logging.getLogger("MCP-Bridge")

# Default page size (check_task_status output lines, list_tasks entries).
_DEFAULT_LIMIT = 64


def handleExecuteTask(ctx, data):
    """Run a Python script from a file path as a tracked task."""
    requestId = data.get("request_id", "unknown")

    scriptPath, err = requireField(data, "script_path", requestId)
    if err:
        return err

    description = data.get("description", "")

    result = ctx.taskRunner.run(scriptPath, description)

    return {"type": "result", "request_id": requestId, **result}


def handleCheckTaskStatus(ctx, data):
    """Return a task's current status with a page of its captured output."""
    requestId = data.get("request_id", "unknown")

    taskId, err = requireField(data, "task_id", requestId)
    if err:
        return err

    skipNewest = data.get("skip_newest", 0)
    limit = data.get("limit", _DEFAULT_LIMIT)
    filterText = data.get("filter_text")

    result = ctx.taskManager.getTaskStatus(
        taskId,
        skipNewest=skipNewest,
        limit=limit,
        filterText=filterText,
    )

    return {"type": "result", "request_id": requestId, **result}


def handleListTasks(ctx, data):
    """List all tracked tasks, newest first."""
    requestId = data.get("request_id", "unknown")
    offset = data.get("offset", 0)
    # An explicit null means "use the default" too; clients page through
    # via offset + pagination.total_count when they want the full history.
    limit = data.get("limit")
    if limit is None:
        limit = _DEFAULT_LIMIT

    result = ctx.taskManager.listAllTasks(offset=offset, limit=limit)

    return {"type": "result", "request_id": requestId, **result}


def handleInterruptTask(ctx, data):
    """Interrupt a running task, or cancel one still waiting in the queue."""
    from ..execution.termination import AsyncAbort, injectAsyncException
    from ..runtime.signals import (
        clearInterrupt,
        getExecThread,
        requestInterrupt,
        unregisterExecThread,
    )

    requestId = data.get("request_id", "unknown")

    taskId, err = requireField(data, "task_id", requestId)
    if err:
        return err

    task = ctx.taskManager.tasks.get(taskId)
    if not task:
        return errorResponse(
            "result",
            requestId,
            "not_found",
            f"Task not found: {taskId}",
            data={"task_id": taskId, "interrupt_requested": False},
        )

    taskStatus = task.status
    if taskStatus not in ("pending", "running"):
        return errorResponse(
            "result",
            requestId,
            "already_terminal",
            f"Task already in terminal state: {taskId} (status: {taskStatus})",
            data={"task_id": taskId, "status": taskStatus, "interrupt_requested": False},
        )

    # Still waiting in the queue: cancel instead of interrupt. If the script
    # just started, cancel() fails and the interrupt path below takes over.
    future = getattr(task, "future", None)
    if taskStatus == "pending" and future is not None and future.cancel():
        logger.info("Queued task canceled: %s", taskId)
        return okResponse(
            "result",
            requestId,
            data={"task_id": taskId, "interrupt_requested": True, "method": "canceled_while_queued"},
        )

    requestInterrupt(taskId)
    logger.info("Interrupt flag set for task: %s", taskId)

    # The flag interrupts a task inside O.run (PyRunner tick → CycleInterrupt);
    # injecting AsyncAbort also covers pure-Python code with no O.run on the
    # stack. Unregister first so a second interrupt cannot inject again.
    method = "flag_only"
    tid = getExecThread(taskId)
    if tid is not None:
        unregisterExecThread(taskId)
        injectAsyncException(tid, AsyncAbort)
        method = "flag_and_async_exc"
        logger.info("AsyncAbort injected into task %s (tid=%s)", taskId, tid)

    # The task may have finished while we set the flag; re-check so the
    # flag cannot leak.
    if task.status not in ("pending", "running"):
        clearInterrupt(taskId)

    dataPayload = {
        "task_id": taskId,
        "interrupt_requested": True,
        "method": method,
    }

    return okResponse("result", requestId, data=dataPayload)
