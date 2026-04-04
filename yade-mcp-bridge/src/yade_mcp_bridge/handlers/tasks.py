"""Task-related message handlers for YADE bridge."""

from .helpers import require_field, truncate_message


async def handle_yade_task(ctx, data):
    """Handle yade_task message - execute Python script from file path."""
    request_id = data.get("request_id", "unknown")

    script_path, err = require_field(data, "script_path", request_id)
    if err:
        return err

    task_id, err = require_field(data, "task_id", request_id)
    if err:
        return err

    description = data.get("description", "")

    result = await ctx.script_runner.run(
        script_path, description, task_id=task_id
    )

    if "message" in result:
        result["message"] = truncate_message(result["message"])

    return {
        "type": "result",
        "request_id": request_id,
        **result
    }


async def handle_check_task_status(ctx, data):
    """Handle check_task_status message."""
    request_id = data.get("request_id", "unknown")

    task_id, err = require_field(data, "task_id", request_id)
    if err:
        return err

    result = ctx.task_manager.get_task_status(task_id)

    if "message" in result:
        result["message"] = truncate_message(result["message"])

    return {
        "type": "result",
        "request_id": request_id,
        **result
    }


async def handle_list_tasks(ctx, data):
    """Handle list_tasks message."""
    request_id = data.get("request_id", "unknown")
    offset = data.get("offset", 0)
    limit = data.get("limit")

    result = ctx.task_manager.list_all_tasks(offset=offset, limit=limit)

    return {
        "type": "result",
        "request_id": request_id,
        **result
    }


async def handle_interrupt_task(ctx, data):
    """Handle interrupt_task message."""
    from ..signals import request_interrupt

    request_id = data.get("request_id", "unknown")

    task_id, err = require_field(data, "task_id", request_id)
    if err:
        return err

    task = ctx.task_manager.tasks.get(task_id)
    if not task:
        return {
            "type": "result",
            "request_id": request_id,
            "status": "error",
            "message": f"Task not found: {task_id}",
            "data": {"task_id": task_id, "interrupt_requested": False}
        }

    task_status = task.status
    if task_status not in ("pending", "running"):
        return {
            "type": "result",
            "request_id": request_id,
            "status": "error",
            "message": f"Task already in terminal state: {task_id} (status: {task_status})",
            "data": {"task_id": task_id, "status": task_status, "interrupt_requested": False}
        }

    request_interrupt(task_id)
    return {
        "type": "result",
        "request_id": request_id,
        "status": "success",
        "message": f"Interrupt requested for task: {task_id}",
        "data": {"task_id": task_id, "interrupt_requested": True}
    }
