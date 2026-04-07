"""Task status query tool backed by yade-mcp-bridge."""

from typing import Any

from fastmcp import FastMCP

from yade_mcp.bridge import get_bridge_client
from yade_mcp.contracts import build_ok
from yade_mcp.formatting import (
    build_bridge_error,
    build_operation_error,
    format_unix_timestamp,
    normalize_status,
    paginate_output,
)
from yade_mcp.utils import FilterText, OutputLimit, SkipNewestLines, TaskId, WaitSeconds


def register(mcp: FastMCP) -> None:
    """Register yade_check_task_status tool."""

    @mcp.tool()
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
            terminal_states = {"completed", "failed", "interrupted", "not_found"}

            # Register listener BEFORE checking status to avoid missing
            # a push notification that arrives between check and wait.
            if wait_seconds > 0:
                client.listen_for_task(task_id)

            response = await client.check_task_status(task_id)
            status = normalize_status(response.get("status", "unknown"))

            if status not in terminal_states and wait_seconds > 0:
                await client.wait_for_task(task_id, timeout=wait_seconds)
                response = await client.check_task_status(task_id)
            else:
                client.unlisten_task(task_id)
        except Exception as exc:
            return build_bridge_error(exc, task_id=task_id)

        status = response.get("status", "unknown")
        if status == "not_found":
            return build_operation_error(
                "not_found",
                "Task not found",
                task_id=task_id,
                action="Verify task_id or submit a new task",
            )

        data = response.get("data") or {}
        normalized_status = normalize_status(status)

        output_text, pagination = paginate_output(
            output=data.get("output") or "",
            skip_newest=skip_newest,
            limit=limit,
            filter_text=filter,
        )

        result: dict[str, Any] = {
            "task_id": task_id,
            "task_status": normalized_status,
            "start_time": format_unix_timestamp(data.get("start_time")),
            "end_time": format_unix_timestamp(data.get("end_time")),
            "elapsed_time": data.get("elapsed_time"),
            "entry_script": data.get("entry_script") or data.get("script_path"),
            "description": data.get("description"),
            "output": output_text,
            "pagination": pagination,
        }

        if data.get("result") is not None:
            result["result"] = data["result"]
        if data.get("error"):
            result["error"] = data["error"]

        return build_ok(result)
