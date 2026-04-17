"""Task interruption tool backed by yade-mcp-bridge."""

from typing import Any

from fastmcp import FastMCP

from yade_mcp.bridge import get_bridge_client
from yade_mcp.bridge.context import with_context
from yade_mcp.contracts import build_ok
from yade_mcp.formatting import build_bridge_error, build_operation_error
from yade_mcp.utils import TaskId


def register(mcp: FastMCP) -> None:
    """Register yade_interrupt_task tool."""

    @mcp.tool()
    @with_context
    async def yade_interrupt_task(task_id: TaskId) -> dict[str, Any]:
        """Request interruption of a running YADE task.

        Two cancellation paths are applied together by the bridge:

        - ``flag_only`` — sets an interrupt flag that YADE's PyRunner
          tick observes between simulation iterations (graceful path
          for ``O.run`` tasks).
        - ``flag_and_async_exc`` — in addition, injects a ``TaskInterrupt``
          exception into the script thread, so pure-Python deadloops
          with no ``O.run`` on the stack are terminated too.

        The response ``method`` field reports which path ran. When
        async-exc is refused (e.g. target thread is a Dummy-N
        boost::python frame), ``async_exc_skipped_reason`` explains why.

        Namespace after interrupt: the YADE ``__main__`` namespace is
        shared between tasks and ``yade_execute_code`` calls. Any
        variables the interrupted script had already defined —
        including ``O`` state — are preserved. There's no need to
        re-run the whole script to continue work: inspect state with
        ``yade_execute_code`` or resume via a fresh ``yade_execute_task``
        that only runs the remaining logic.
        """
        try:
            client = await get_bridge_client()
            response = await client.interrupt_task(task_id)
        except Exception as exc:
            return build_bridge_error(exc, task_id=task_id)

        status = response.get("status", "unknown")
        message = response.get("message", "")
        bridge_data = response.get("data") or {}

        if status == "success":
            payload: dict[str, Any] = {
                "task_id": task_id,
                "interrupt_requested": True,
                "message": message or "signal sent",
                "next_action": f'call yade_check_task_status(task_id="{task_id}")',
            }
            # Surface the bridge's cancellation metadata to the agent.
            for key in (
                "method",
                "async_exc_skipped_reason",
                "namespace_preserved",
                "continuation_hint",
            ):
                if key in bridge_data:
                    payload[key] = bridge_data[key]
            return build_ok(payload)

        return build_operation_error(
            status or "interrupt_failed",
            message or "Interrupt request failed",
            task_id=task_id,
            action="Check task status and bridge logs",
        )
