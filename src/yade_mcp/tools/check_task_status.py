"""Task status query tool backed by yade-mcp-bridge."""

from typing import Any

from fastmcp import FastMCP

from yade_mcp.bridge import get_bridge_client
from yade_mcp.bridge.context import with_context
from yade_mcp.contracts import build_ok
from yade_mcp.formatting import (
    build_bridge_error,
    build_operation_error,
    format_unix_timestamp,
    normalize_status,
    round_elapsed_seconds,
)
from yade_mcp.utils import FilterText, OutputLimit, SkipNewestLines, TaskId, WaitSeconds


def _lifecycle_status(response: dict[str, Any]) -> str:
    """Read a task's lifecycle status: canonical ``data.status``, legacy top-level fallback."""
    data = response.get("data") or {}
    return str(data.get("status") or response.get("status") or "unknown")


def register(mcp: FastMCP) -> None:
    """Register yade_check_task_status tool."""

    @mcp.tool()
    @with_context
    async def yade_check_task_status(
        task_id: TaskId,
        skip_newest: SkipNewestLines = 0,
        limit: OutputLimit = 64,
        filter: FilterText = None,
        wait_seconds: WaitSeconds = 1,
    ) -> dict[str, Any]:
        """Check status and output for a submitted YADE task."""
        try:
            client = await get_bridge_client()
            terminal_states = {"completed", "failed", "interrupted", "canceled", "not_found"}

            # Register listener BEFORE checking status to avoid missing
            # a push notification that arrives between check and wait.
            if wait_seconds > 0:
                client.listen_for_task(task_id)

            response = await client.check_task_status(
                task_id,
                skip_newest=skip_newest,
                limit=limit,
                filter_text=filter,
            )
            # A request-level error (e.g. not_found) is terminal — never poll.
            is_terminal = (
                bool(response.get("error")) or normalize_status(_lifecycle_status(response)) in terminal_states
            )

            if not is_terminal and wait_seconds > 0:
                await client.wait_for_task(task_id, timeout=wait_seconds)
                response = await client.check_task_status(
                    task_id,
                    skip_newest=skip_newest,
                    limit=limit,
                    filter_text=filter,
                )
            else:
                client.unlisten_task(task_id)
        except Exception as exc:
            return build_bridge_error(exc, task_id=task_id)

        # Request-level failure: lift the bridge's machine-readable error code;
        # legacy bridges sent status:"not_found" with no error object.
        bridge_error = response.get("error") or {}
        if bridge_error or response.get("status") == "not_found":
            return build_operation_error(
                bridge_error.get("code", "not_found"),
                "Task not found",
                task_id=task_id,
                action="Verify task_id or submit a new task",
            )

        # Lifecycle path: the task's status is domain info, not a request error.
        data = response.get("data") or {}
        normalized_status = normalize_status(_lifecycle_status(response))

        bridge_output = data.get("output") or ""
        output_text = bridge_output if bridge_output else "(no output)"
        pagination = data.get("pagination") or {
            "total_lines": 0,
            "line_range": "0-0",
        }

        result: dict[str, Any] = {
            "task_id": task_id,
            "task_status": normalized_status,
            "start_time": format_unix_timestamp(data.get("start_time")),
            "end_time": format_unix_timestamp(data.get("end_time")),
            "elapsed_time": round_elapsed_seconds(data.get("elapsed_time")),
            "script_path": data.get("script_path") or data.get("entry_script"),
            "description": data.get("description"),
            "output": output_text,
            "pagination": pagination,
        }

        # A pending task is waiting in the bridge's FIFO queue, not stuck;
        # point the agent at the queue view instead of leaving it guessing.
        if normalized_status == "pending":
            result["note"] = (
                "Task is queued; tasks run one at a time in submit order. "
                "Use yade_list_tasks to see what is running ahead of it."
            )

        if data.get("result") is not None:
            result["result"] = data["result"]
        if data.get("error"):
            result["error"] = data["error"]
        # Failed-task diagnostics ride under their own key (the tool call
        # itself succeeded), keeping them clear of the prod-mode details strip.
        if data.get("error_details"):
            result["error_details"] = data["error_details"]

        return build_ok(result)
