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
    """Read a task's lifecycle status from a bridge response.

    Canonical location is ``data.status`` (the status describes the task, so
    the bridge nests it in the task data). Older bridges put it at the
    envelope top level, so fall back there for cross-version compatibility —
    that fallback is transitional and can be dropped once the in-tree wire
    floor moves past top-level status.
    """
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
            terminal_states = {"completed", "failed", "interrupted", "not_found"}

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
            # Detect it via the structured error, falling back to the legacy
            # not_found status string for older bridges.
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

        # Request-level failure: bridge sends a structured error{}. Lift its
        # machine-readable code; keep the MCP-composed remediation action.
        # (Legacy bridges sent status:"not_found" with no error object.)
        bridge_error = response.get("error") or {}
        if bridge_error or response.get("status") == "not_found":
            return build_operation_error(
                bridge_error.get("code", "not_found"),
                "Task not found",
                task_id=task_id,
                action="Verify task_id or submit a new task",
            )

        # Lifecycle path (running / completed / failed / interrupted): the
        # task's status is genuine domain info, read from data.status (bridge
        # nests it; older bridges put it top-level — see _lifecycle_status).
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
            "entry_script": data.get("entry_script") or data.get("script_path"),
            "description": data.get("description"),
            "output": output_text,
            "pagination": pagination,
        }

        if data.get("result") is not None:
            result["result"] = data["result"]
        if data.get("error"):
            result["error"] = data["error"]
        # Structured traceback / exception type / overflow log pointer
        # for failed tasks. Surfaced as a sibling of `error` (not inside
        # it) because check_task_status returns an ok envelope — the
        # tool call itself succeeded; the *task* failed. Nests under a
        # separate key so the prod-mode details strip (on tool errors)
        # never touches it.
        if data.get("error_details"):
            result["error_details"] = data["error_details"]

        return build_ok(result)
