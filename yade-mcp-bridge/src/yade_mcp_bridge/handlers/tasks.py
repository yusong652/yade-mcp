# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Task-related message handlers for MCP bridge."""

import logging

from ..utils import error_response, ok_response
from .helpers import require_field

logger = logging.getLogger("MCP-Bridge")

# Default page size for the paginated reads (check_task_status output lines,
# list_tasks entries). Callers that omit ``limit`` get a bounded page instead
# of the full history; ``pagination.total_count`` tells them what remains.
_DEFAULT_LIMIT = 64


def handle_execute_task(ctx, data):
    """Run a Python script from a file path as a tracked task."""
    request_id = data.get("request_id", "unknown")

    script_path, err = require_field(data, "script_path", request_id)
    if err:
        return err

    task_id, err = require_field(data, "task_id", request_id)
    if err:
        return err

    description = data.get("description", "")

    result = ctx.script_runner.run(script_path, description, task_id=task_id)

    return {"type": "result", "request_id": request_id, **result}


def handle_check_task_status(ctx, data):
    """Return a task's current status with a page of its captured output."""
    request_id = data.get("request_id", "unknown")

    task_id, err = require_field(data, "task_id", request_id)
    if err:
        return err

    skip_newest = data.get("skip_newest", 0)
    limit = data.get("limit", _DEFAULT_LIMIT)
    filter_text = data.get("filter_text")

    result = ctx.task_manager.get_task_status(
        task_id,
        skip_newest=skip_newest,
        limit=limit,
        filter_text=filter_text,
    )

    return {"type": "result", "request_id": request_id, **result}


def handle_list_tasks(ctx, data):
    """List all tracked tasks, newest first."""
    request_id = data.get("request_id", "unknown")
    offset = data.get("offset", 0)
    # An explicit null means "use the default" too; clients page through
    # via offset + pagination.total_count when they want the full history.
    limit = data.get("limit")
    if limit is None:
        limit = _DEFAULT_LIMIT

    result = ctx.task_manager.list_all_tasks(offset=offset, limit=limit)

    return {"type": "result", "request_id": request_id, **result}


def handle_interrupt_task(ctx, data):
    """Interrupt a running task."""
    from ..execution.errors import TaskInterrupt
    from ..execution.termination import inject_async_exception
    from ..runtime.signals import (
        clear_interrupt,
        get_exec_thread,
        request_interrupt,
        unregister_exec_thread,
    )

    request_id = data.get("request_id", "unknown")

    task_id, err = require_field(data, "task_id", request_id)
    if err:
        return err

    task = ctx.task_manager.tasks.get(task_id)
    if not task:
        return error_response(
            "result",
            request_id,
            "not_found",
            f"Task not found: {task_id}",
            data={"task_id": task_id, "interrupt_requested": False},
        )

    task_status = task.status
    if task_status not in ("pending", "running"):
        return error_response(
            "result",
            request_id,
            "already_terminal",
            f"Task already in terminal state: {task_id} (status: {task_status})",
            data={"task_id": task_id, "status": task_status, "interrupt_requested": False},
        )

    request_interrupt(task_id)
    logger.info("Interrupt flag set for task: %s", task_id)

    # Best-effort async injection. Atomic unregister-then-inject prevents
    # a re-entrant interrupt from landing a second TaskInterrupt in the
    # middle of the script thread's except-block cleanup.
    method = "flag_only"
    tid = get_exec_thread(task_id)
    if tid is not None:
        unregister_exec_thread(task_id)
        inject_async_exception(tid, TaskInterrupt)
        method = "flag_and_async_exc"
        logger.info("Async TaskInterrupt injected into task %s (tid=%s)", task_id, tid)

    # Defend against TOCTOU: task may have finished between the status check
    # above and request_interrupt. script_runner.py's finally clears the flag on exit,
    # but if we add it after that runs, the flag would leak. Re-check and clean.
    if task.status not in ("pending", "running"):
        clear_interrupt(task_id)

    data_payload = {
        "task_id": task_id,
        "interrupt_requested": True,
        "method": method,
        "namespace_preserved": True,
        "continuation_hint": (
            "Task variables and YADE state are preserved in __main__. "
            "Use yade_execute_code to inspect (e.g. O.iter, len(O.bodies), "
            "local vars defined by the script) or to run the remaining "
            "logic directly. If more simulation iters are needed, submit "
            "a fresh yade_execute_task with a short continuation script."
        ),
    }

    return ok_response("result", request_id, data=data_payload)
