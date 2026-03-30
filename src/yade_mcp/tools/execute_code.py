"""YADE execute_code tool — synchronous code execution in YADE process."""

from typing import Any

from fastmcp import FastMCP

from yade_mcp.bridge import get_bridge_client
from yade_mcp.contracts import build_ok
from yade_mcp.formatting import build_bridge_error, build_operation_error, is_bridge_connectivity_error
from yade_mcp.utils import ConsoleCode, ConsoleTimeoutSeconds


def register(mcp: FastMCP) -> None:
    """Register yade_execute_code tool."""

    @mcp.tool()
    async def yade_execute_code(
        code: ConsoleCode,
        timeout: ConsoleTimeoutSeconds = 10,
    ) -> dict[str, Any]:
        """Execute Python code synchronously in the running YADE process.

        Returns stdout immediately. Code runs in the YADE Python
        environment where yade modules are already imported;
        side effects persist.

        Typical uses:
        - Query simulation state: O.bodies count, current iteration
        - Create/modify bodies, engines, interactions
        - Read or set material properties
        - Development and REPL-style testing

        Unlike yade_execute_task, this tool is fire-and-return: the
        response contains the full output. It is NOT tracked by
        yade_list_tasks and cannot be interrupted or polled.

        WARNING: Avoid long-running calls (O.run with many iterations,
        heavy loops). They block until completion or timeout and cannot
        be cancelled. Use yade_execute_task for long simulations.
        """
        try:
            client = await get_bridge_client()
            response = await client.execute_code(
                code=code,
                timeout_ms=timeout * 1000,
            )
        except Exception as exc:
            if is_bridge_connectivity_error(exc):
                return build_bridge_error(exc)
            return build_operation_error(
                "execute_code_failed",
                "Code execution failed",
                reason=str(exc),
            )

        status = response.get("status", "unknown")
        message = response.get("message", "")

        if status == "timeout":
            return build_operation_error(
                "timeout",
                "Execution timed out",
                reason=message,
                action="Reduce code complexity or increase timeout",
            )

        if status == "error":
            error = response.get("error") or {}
            return build_operation_error(
                error.get("code", "execute_code_error"),
                error.get("message", message),
                reason=message,
            )

        data = response.get("data") or {}
        result_data: dict[str, Any] = {
            "output": data.get("output") or "(no output)",
        }
        if data.get("result") is not None:
            result_data["result"] = data["result"]

        return build_ok(result_data)
