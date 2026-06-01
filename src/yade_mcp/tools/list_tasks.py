"""Task listing tool backed by yade-mcp-bridge."""

from typing import Any

from fastmcp import FastMCP

from yade_mcp.bridge import get_bridge_client
from yade_mcp.bridge.context import with_context
from yade_mcp.contracts import build_ok
from yade_mcp.formatting import build_bridge_error, build_operation_error, format_unix_timestamp, normalize_status
from yade_mcp.utils import SkipNewestTasks, TaskListLimit


def register(mcp: FastMCP) -> None:
    """Register yade_list_tasks tool."""

    @mcp.tool()
    @with_context
    async def yade_list_tasks(
        skip_newest: SkipNewestTasks = 0,
        limit: TaskListLimit = 32,
    ) -> dict[str, Any]:
        """List tracked YADE tasks with pagination."""
        try:
            client = await get_bridge_client()
            response = await client.list_tasks(
                offset=skip_newest,
                limit=limit,
            )
        except Exception as exc:
            return build_bridge_error(exc)

        ok = response.get("ok")
        if ok is None:
            # Legacy bridge: success was signalled by status:"success".
            ok = response.get("status") == "success"
        if not ok:
            bridge_error = response.get("error") or {}
            return build_operation_error(
                bridge_error.get("code") or response.get("status") or "list_failed",
                bridge_error.get("message") or response.get("message") or "Failed to list tasks",
                action="Check bridge state and retry",
            )

        tasks = response.get("data") or []
        pagination = response.get("pagination") or {}
        total_count = pagination.get("total_count", len(tasks))
        displayed_count = pagination.get("displayed_count", len(tasks))
        has_more = pagination.get("has_more", False)

        normalized_tasks: list[dict[str, Any]] = []

        for task in tasks:
            normalized_task = {
                "task_id": task.get("task_id"),
                "status": normalize_status(task.get("status", "unknown")),
                "source": task.get("source", "agent"),
                "start_time": format_unix_timestamp(task.get("start_time")),
                "end_time": format_unix_timestamp(task.get("end_time")),
                "elapsed_time": task.get("elapsed_time"),
                "entry_script": task.get("entry_script"),
                "description": task.get("description"),
            }
            normalized_tasks.append(normalized_task)

        return build_ok(
            {
                "total_count": total_count,
                "displayed_count": displayed_count,
                "has_more": has_more,
                "tasks": normalized_tasks,
            }
        )
