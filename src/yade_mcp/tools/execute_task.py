"""YADE task execution tool backed by yade-mcp-bridge."""

from typing import Any

from fastmcp import FastMCP

from yade_mcp.bridge import get_bridge_client
from yade_mcp.bridge.context import with_context
from yade_mcp.contracts import build_ok
from yade_mcp.formatting import build_bridge_error, build_operation_error
from yade_mcp.utils import ScriptPath, TaskDescription


def register(mcp: FastMCP) -> None:
    """Register yade_execute_task tool."""

    @mcp.tool()
    @with_context
    async def yade_execute_task(
        script_path: ScriptPath,
        description: TaskDescription,
    ) -> dict[str, Any]:
        """Submit a Python script for asynchronous execution in YADE.

        Returns a task_id immediately; the script is queued and runs in
        the background. Tasks run one at a time in submit order (they
        share the YADE process and its single simulation state), so a
        multi-stage pipeline can be submitted in one go — each stage
        starts when the previous one finishes. Use the companion tools
        to manage the task lifecycle:
        - yade_check_task_status: poll output, progress, and final status
        - yade_interrupt_task: stop a running task or cancel a queued one
        - yade_list_tasks: browse task history (also shows queue order)

        Use this for production simulation runs, long O.run() cycles,
        and any operation that may take minutes or longer.
        For quick queries and REPL-style testing, use yade_execute_code.
        """
        try:
            client = await get_bridge_client()
        except Exception as exc:
            return build_bridge_error(exc)

        try:
            response = await client.execute_task(
                script_path=script_path,
                description=description,
            )
        except Exception as exc:
            # Connected but request failed — task may or may not exist on bridge
            return build_bridge_error(exc)

        bridge_error = response.get("error") or {}
        ok = response.get("ok")
        if ok is None:
            # Legacy bridge: submit success was signalled by status:"pending"
            # (and submit failures by a bare status:"error").
            ok = response.get("status") == "pending"

        # Request-level failure: a structured error{} (missing_field,
        # script_not_found, script_read_error, submit_failed) — or a legacy
        # bridge's bare status:"error". Lift the machine-readable code.
        if bridge_error or not ok:
            return build_operation_error(
                bridge_error.get("code") or response.get("status") or "submission_failed",
                bridge_error.get("message") or response.get("message") or "Task submission rejected by bridge",
                action="Check script path and bridge logs, then retry",
            )

        # The bridge assigns the task_id and returns it with the submission.
        response_data = response.get("data") or {}
        return build_ok(
            {
                "task_id": response_data.get("task_id"),
                "script_path": script_path,
                "description": description,
                "task_status": "pending",
                "message": "submitted",
            }
        )
