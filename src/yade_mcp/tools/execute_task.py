"""YADE task execution tool backed by yade-mcp-bridge."""

import uuid
from typing import Any

from fastmcp import FastMCP

from yade_mcp.bridge import get_bridge_client
from yade_mcp.contracts import build_ok
from yade_mcp.formatting import build_bridge_error, build_operation_error
from yade_mcp.utils import ScriptPath, TaskDescription


def register(mcp: FastMCP) -> None:
    """Register yade_execute_task tool."""

    @mcp.tool()
    async def yade_execute_task(
        entry_script: ScriptPath,
        description: TaskDescription,
    ) -> dict[str, Any]:
        """Submit a Python script for asynchronous execution in YADE.

        Returns a task_id immediately; the script runs in the background.
        Use the companion tools to manage the task lifecycle:
        - yade_check_task_status: poll output, progress, and final status
        - yade_interrupt_task: cancel a running task
        - yade_list_tasks: browse task history

        Use this for production simulation runs, long O.run() cycles,
        and any operation that may take minutes or longer.
        For quick queries and REPL-style testing, use yade_execute_code.
        """
        try:
            client = await get_bridge_client()
        except Exception as exc:
            # Connection failed — no task_id generated, nothing to track
            return build_bridge_error(exc)

        task_id = uuid.uuid4().hex[:6]

        try:
            response = await client.execute_task(
                script_path=entry_script,
                description=description,
                task_id=task_id,
            )
        except Exception as exc:
            # Connected but request failed — task may or may not exist on bridge
            return build_bridge_error(exc, task_id=task_id)

        status = response.get("status", "unknown")
        message = response.get("message", "")

        if status != "pending":
            return build_operation_error(
                status or "submission_failed",
                message or "Task submission rejected by bridge",
                task_id=task_id,
                action="Check script path and bridge logs, then retry",
            )

        return await build_ok(
            {
                "task_id": task_id,
                "entry_script": entry_script,
                "description": description,
                "task_status": "pending",
                "message": message or "submitted",
            }
        )
