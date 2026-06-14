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
    """Handle execute_task message - execute Python script from file path."""
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
    """Handle check_task_status message with bridge-side output pagination."""
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
    """Handle list_tasks message."""
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
    """Handle interrupt_task message.

    Two cancellation paths, applied together:

    1. **Flag path** — sets ``_interrupt_requested[task_id]=True``. The
       bridge's PyRunner tick (on YADE's sim thread) sees the flag at
       the next iteration and calls ``O.pause()``; ``ScriptRunner._execute``
       then raises ``InterruptedError`` after ``O.run`` returns. This is
       the graceful path for simulation-bound tasks.

    2. **Async-exc path** — injects ``TaskInterrupt`` into the script
       thread via ``PyThreadState_SetAsyncExc``. Covers the case the
       flag path cannot reach: pure-Python deadloops with no ``O.run``
       on the stack, so no PyRunner tick ever fires.

    Both are safe to apply together. The async-exc injection
    ``unregister_exec_thread`` atomically *before* firing — any
    duplicate interrupt call sees ``tid=None`` and becomes a no-op,
    so the script thread's cleanup runs without being re-interrupted.
    """
    from ..execution.errors import TaskInterrupt
    from ..execution.termination import fire_async_exception, is_safe_to_async_raise
    from ..signals import (
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

    # Best-effort async injection. Atomic unregister-then-fire prevents
    # a re-entrant interrupt from landing a second TaskInterrupt in the
    # middle of the script thread's except-block cleanup.
    method = "flag_only"
    reason = None
    tid = get_exec_thread(task_id)
    if tid is not None:
        safe, safe_reason = is_safe_to_async_raise(tid)
        if safe:
            unregister_exec_thread(task_id)
            fire_async_exception(tid, TaskInterrupt)
            method = "flag_and_async_exc"
            logger.info("Async TaskInterrupt injected into task %s (tid=%s)", task_id, tid)
        else:
            method = "flag_only"
            reason = safe_reason
            logger.info("Task %s async_exc refused (%s); flag-only path in effect", task_id, safe_reason)

    # Defend against TOCTOU: task may have finished between the status check
    # above and request_interrupt. script.py's finally clears the flag on exit,
    # but if we add it after that runs, the flag would leak. Re-check and clean.
    if task.status not in ("pending", "running"):
        clear_interrupt(task_id)

    data_payload = {
        "task_id": task_id,
        "interrupt_requested": True,
        "method": method,
        # Continuation hint: the YADE __main__ namespace is shared
        # between execute_code and tasks (see CLAUDE.md "Bridge
        # namespace sharing"). Variables defined by the interrupted
        # script — including O's state — survive the interrupt, so
        # there's no need to re-run the whole script to pick up.
        "namespace_preserved": True,
        "continuation_hint": (
            "Task variables and YADE state are preserved in __main__. "
            "Use yade_execute_code to inspect (e.g. O.iter, len(O.bodies), "
            "local vars defined by the script) or to run the remaining "
            "logic directly. If more simulation iters are needed, submit "
            "a fresh yade_execute_task with a short continuation script."
        ),
    }
    if reason is not None:
        data_payload["async_exc_skipped_reason"] = reason

    return ok_response("result", request_id, data=data_payload)
