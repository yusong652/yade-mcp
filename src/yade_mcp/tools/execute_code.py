"""YADE execute_code tool — synchronous code execution in YADE process."""

from typing import Any

from fastmcp import FastMCP

from yade_mcp.bridge import get_bridge_client
from yade_mcp.bridge.context import with_context
from yade_mcp.contracts import build_ok
from yade_mcp.formatting import build_bridge_error, build_operation_error, is_bridge_connectivity_error
from yade_mcp.utils import ConsoleCode, ConsoleTimeoutSeconds


def register(mcp: FastMCP) -> None:
    """Register yade_execute_code tool."""

    @mcp.tool()
    @with_context
    async def yade_execute_code(
        code: ConsoleCode,
        timeout: ConsoleTimeoutSeconds = 10,
    ) -> dict[str, Any]:
        """Execute Python code synchronously in the running YADE process.

        Returns stdout immediately. Code runs in the YADE Python
        environment where yade modules are already imported;
        side effects persist.

        This tool remains responsive EVEN WHILE a simulation task is
        running (submitted via yade_execute_task). Use it as a live
        REPL to inspect simulation state in real time — no need to
        pre-script print statements.

        Typical uses:
        - Query simulation state: O.bodies count, current iteration
        - Create/modify bodies, engines, interactions
        - Read or set material properties
        - Live inspection during a running simulation (e.g. check
          stress tensor, coordination number, energy balance,
          or capture viewport screenshots when GUI is available)
        - Development and REPL-style testing

        Unlike yade_execute_task, this tool is fire-and-return: the
        response contains the full output. It is NOT tracked by
        yade_list_tasks and cannot be interrupted or polled.

        Timeout behaviour: on timeout the bridge attempts to abort the
        running code. The response is an error envelope (``ok=false``)
        whose ``error.code`` is one of:

        - ``interrupted`` — the code was running a simulation cycle
          (``O.run``) and was paused cleanly at an iteration boundary.
          For long simulations or solving to equilibrium, switch to
          yade_execute_task — it tracks progress and stops cleanly via
          yade_interrupt_task.
        - ``terminated`` — a non-cycle abort succeeded (async exception
          injection); the pump thread is free, but YADE state may be
          partially modified by the code that ran before the abort fired.
          Inspect state before retrying.
        - ``timeout`` — abort failed (code stuck in a C extension, or
          nested inside a running task's PyRunner tick); the bridge may
          still be blocked. Restart if unresponsive.

        WARNING: For anything expected to take more than a few seconds,
        use yade_execute_task instead — it has proper cancellation via
        yade_interrupt_task and does not leave state drift on timeout.
        Also, do NOT write ``except BaseException:`` in your code; it
        defeats bridge-initiated cancellation.
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

        bridge_data = response.get("data") or {}
        bridge_error = response.get("error") or {}
        bridge_details = bridge_error.get("details") or {}

        # Success/failure axis: the bridge now signals via ``ok`` (bool).
        # Fall back to the legacy ``status`` string for bridges that predate
        # the ok envelope (cross-version compat, same rationale as
        # ``normalize_status`` — see project_semver).
        ok = response.get("ok")
        if ok is None:
            ok = response.get("status") == "success"

        if ok:
            result_data: dict[str, Any] = {
                "output": bridge_data.get("output") or "(no output)",
            }
            if bridge_data.get("result") is not None:
                result_data["result"] = bridge_data["result"]
            return build_ok(result_data)

        # Failure: branch on the machine-readable ``error.code``. The human
        # message lives in ``error.message``; fall back to a legacy top-level
        # ``message`` for responses that don't yet carry a structured error
        # (e.g. the missing-field request error — pending its own cleanup).
        code = bridge_error.get("code", "execute_code_error")
        message = bridge_error.get("message") or response.get("message", "")

        if code == "interrupted":
            # Bridge cleanly paused a standalone O.run cycle that blew the
            # timeout. Pull the agent back to the task tool, which is built
            # for long-running simulations.
            partial_output = bridge_data.get("output")
            return build_operation_error(
                "interrupted",
                "Simulation cycle interrupted on timeout",
                reason=message,
                action=(
                    "execute_code ran an O.run simulation cycle that "
                    "exceeded the timeout; the bridge paused it at an "
                    "iteration boundary. For long simulations or solving "
                    "to equilibrium use yade_execute_task — it tracks "
                    "progress and can be cleanly stopped via "
                    "yade_interrupt_task."
                ),
                data={"output": partial_output} if partial_output else None,
                method=bridge_details.get("method"),
            )

        if code == "terminated":
            # Bridge successfully aborted the code. Surface the
            # partial stdout and the termination method so the agent
            # knows state may be partially modified.
            partial_output = bridge_data.get("output")
            return build_operation_error(
                "terminated",
                "Execution timed out and was aborted",
                reason=message,
                action=(
                    "YADE state may be partially modified by the "
                    "aborted code. Inspect via yade_execute_code "
                    "before retrying; prefer yade_execute_task for "
                    "long operations."
                ),
                data={"output": partial_output} if partial_output else None,
                method=bridge_details.get("method"),
            )

        if code == "timeout":
            # Abort failed — pump may still be blocked.
            return build_operation_error(
                "timeout",
                "Execution timed out and abort failed",
                reason=message,
                action=(
                    "The code may still be running (stuck in C "
                    "extension or nested callback). Restart the "
                    "bridge if subsequent execute_code calls hang."
                ),
                method=bridge_details.get("method"),
                stuck_reason=bridge_details.get("reason"),
            )

        partial_output = bridge_data.get("output")
        return build_operation_error(
            code,
            message,
            data={"output": partial_output} if partial_output else None,
            exception_type=bridge_details.get("exception_type"),
            traceback=bridge_details.get("traceback"),
            traceback_truncated=bridge_details.get("traceback_truncated"),
            log_file=bridge_details.get("log_file"),
        )
