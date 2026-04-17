"""Execute code message handler for YADE bridge.

Handles synchronous code snippet execution via main thread queue.
"""

import asyncio
import logging
import os
import sys
import threading
import time
from io import StringIO

from ..execution.errors import BridgeTimeout, format_execution_error
from ..execution.termination import fire_async_exception, is_safe_to_async_raise
from ..signals import (
    clear_interrupt,
    get_exec_thread,
    register_exec_thread,
    request_interrupt,
    unregister_exec_thread,
)
from ..utils import TeeBuffer
from .helpers import require_field

logger = logging.getLogger("YADE-Bridge")

# How long to wait for the pump thread to unwind after we inject
# ``BridgeTimeout``. A pure-Python loop aborts within a handful of
# bytecode instructions (milliseconds); 0.5s covers worst-case
# user-code ``finally`` blocks without letting "stuck in C" cases
# stall the handler response.
_TERMINATION_GRACE_S = 0.5


async def _terminate_stuck_execution(request_id: str, future) -> dict:
    """Best-effort cancellation of an ``execute_code`` submission that
    blew its timeout.

    Returns a dict summarising the outcome:

    * ``resolved``: bool — did the pump thread settle the future within
      the grace period? When True, the pump is free and the bridge is
      healthy; when False, the pump may still be blocked.
    * ``method``: one of ``"self"`` (future already resolved before we
      got here), ``"async_exc"`` (SetAsyncExc succeeded), ``"flag_only"``
      (couldn't SetAsyncExc — nested or qt-main; fell back to the flag),
      ``"stuck_in_c"`` (SetAsyncExc fired but pump didn't respond in
      grace period — likely in a C extension).
    * ``reason``: machine-readable reason when ``method == "flag_only"``.
    * ``result``: the future's result dict when resolved, else None.
    """
    # Always fire the flag first — cheap, helps if code is inside
    # ``O.run(wait=True)`` (PyRunner tick → O.pause → O.run returns).
    request_interrupt(request_id)

    tid = get_exec_thread(request_id)

    # Registry already cleared → _execute_code's finally ran → the
    # pump is free. The future should be resolved or about to be.
    if tid is None:
        if future.done():
            return {"resolved": True, "method": "self", "result": future.result()}
        return {"resolved": False, "method": "self", "result": None}

    safe, reason = is_safe_to_async_raise(tid)
    if not safe:
        # Can't safely inject (Dummy-N nested case, or Qt main thread
        # in GUI mode). Fall back to flag-only cancellation.
        if future.done():
            return {
                "resolved": True,
                "method": "flag_only",
                "reason": reason,
                "result": future.result(),
            }
        return {
            "resolved": False,
            "method": "flag_only",
            "reason": reason,
            "result": None,
        }

    fire_async_exception(tid, BridgeTimeout)

    try:
        result = await asyncio.wait_for(
            asyncio.wrap_future(future), timeout=_TERMINATION_GRACE_S
        )
        return {"resolved": True, "method": "async_exc", "result": result}
    except (asyncio.TimeoutError, TimeoutError):
        return {"resolved": False, "method": "stuck_in_c", "result": None}


def _timeout_response(request_id: str, timeout_ms: int, termination: dict) -> dict:
    """Build the wire response for an ``execute_code`` that timed out.

    * ``resolved=True`` → status ``"terminated"``: pump thread is free,
      but YADE state may be partially modified by whatever the user
      code wrote before the abort.
    * ``resolved=False`` → status ``"timeout"``: cancellation couldn't
      complete; pump may still be blocked.
    """
    resolved = termination["resolved"]
    method = termination["method"]
    result = termination.get("result")

    # Output captured up to the abort point (present on async_exc path
    # and on self-resolve path; empty otherwise).
    output = ""
    if isinstance(result, dict):
        output = result.get("output", "") or ""

    if resolved:
        status = "terminated"
        message = (
            f"Execution timed out after {timeout_ms}ms and was aborted. "
            "YADE state may be partially modified by the aborted code; "
            "inspect via yade_execute_code before retrying."
        )
        error_code = "terminated"
    else:
        status = "timeout"
        if method == "flag_only":
            message = (
                f"Execution timed out after {timeout_ms}ms. Full abort "
                f"was not possible ({termination.get('reason')}); the "
                "code may still be running in the background."
            )
        elif method == "stuck_in_c":
            message = (
                f"Execution timed out after {timeout_ms}ms. Bridge "
                "failed to terminate the code — it is likely stuck in "
                "a C extension (e.g. numpy/scipy). The bridge may "
                "recover when the C call returns; otherwise restart."
            )
        else:
            # self / self_untracked without resolved — defensive.
            message = f"Execution timed out after {timeout_ms}ms."
        error_code = "timeout"

    details = {"method": method}
    if "reason" in termination:
        details["reason"] = termination["reason"]

    return {
        "type": "execute_code_result",
        "request_id": request_id,
        "status": status,
        "message": message,
        "error": {
            "code": error_code,
            "message": message,
            "details": details,
        },
        "data": {"output": output},
    }


async def handle_execute_code(ctx, data):
    """Handle execute_code message - run code synchronously in YADE."""

    request_id = data.get("request_id", "unknown")

    code, err = require_field(data, "code", request_id, "execute_code_result")
    if err:
        return err

    timeout_ms = data.get("timeout_ms", 10000)
    timeout_s = timeout_ms / 1000.0

    try:

        def _execute_code(code_str):
            """Execute code in main thread, capturing stdout.

            Registers the running thread id so that a timeout in the
            surrounding handler can terminate this body via
            ``PyThreadState_SetAsyncExc(BridgeTimeout)``. The
            ``except BridgeTimeout`` branch here is LOAD-BEARING:
            ``BridgeTimeout`` inherits ``BaseException`` and must not
            escape ``_execute_code``. If it did, it would slip past
            ``MainThreadExecutor.process_tasks``'s ``except Exception``
            (which doesn't catch BaseException) and kill the pump
            thread permanently.

            We intentionally do NOT touch ``_current_task_id``. That
            global is read by ``_mcp_pyrunner_tick``'s no-arg
            ``is_interrupt_requested()`` check. If we overwrote it with
            ``request_id``, a subsequent ``_terminate_stuck_execution``
            setting the REPL's own interrupt flag would be misread by
            PyRunner tick as a task interrupt — ``O.pause()`` + the
            ``_hooked_run`` InterruptedError raise, wrongly terminating
            the outer task with status=interrupted. ``execute_code``
            has no flag-based cancellation anyway (it uses async_exc).
            """
            output_buffer = StringIO()
            old_stdout = sys.stdout

            register_exec_thread(request_id, threading.get_ident())

            try:
                # stdout redirect lives inside the try so ``finally``'s
                # restoration is always paired with a successful assignment.
                terminal = sys.__stdout__ if sys.__stdout__ is not None else old_stdout
                sys.stdout = TeeBuffer(terminal, output_buffer)

                import __main__

                exec_globals = __main__.__dict__
                exec_globals.pop("result", None)

                try:
                    code_obj = compile(code_str, "<execute_code>", "eval")
                    result = eval(code_obj, exec_globals, exec_globals)
                except SyntaxError:
                    code_obj = compile(code_str, "<execute_code>", "exec")
                    exec(code_obj, exec_globals, exec_globals)
                    result = exec_globals.get("result", None)

                output_text = output_buffer.getvalue()
                return {
                    "status": "success",
                    "output": output_text,
                    "result": result
                    if isinstance(result, (str, int, float, bool, list, dict, type(None)))
                    else str(result)
                    if result is not None
                    else None,
                }
            except BridgeTimeout:
                # Bridge-initiated termination. Return a marker so the
                # outer handler reports status="terminated" rather than
                # treating this as a user error. Critical: do NOT let
                # this exception escape — see docstring.
                return {
                    "status": "terminated",
                    "output": output_buffer.getvalue(),
                }
            except Exception as e:
                output_text = output_buffer.getvalue()
                # Suppress chained-exception noise. The handler tries
                # compile(..., "eval") first and falls back to exec on
                # SyntaxError; that fallback leaves a "During handling
                # of the above exception..." preamble in format_exc
                # that's pure plumbing, not user-relevant.
                e.__suppress_context__ = True

                def _overflow_writer(full_tb: str) -> str:
                    # Only called when the excerpt is truncated. Fresh
                    # timestamped file per error — execute_code is
                    # synchronous REPL, overwriting a single rolling file
                    # would race if another call lands between error and
                    # agent read.
                    log_dir = os.path.join(".yade-mcp", "logs")
                    os.makedirs(log_dir, exist_ok=True)
                    path = os.path.join(log_dir, f"exec_code_error_{int(time.time() * 1000)}.log")
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(full_tb)
                    return os.path.abspath(path)

                return format_execution_error(
                    e,
                    output_text,
                    is_user_frame=lambda fn: fn == "<execute_code>",
                    display_path="<execute_code>",
                    message_prefix="Code execution failed",
                    overflow_writer=_overflow_writer,
                )
            finally:
                sys.stdout = old_stdout
                clear_interrupt(request_id)
                unregister_exec_thread(request_id)

        # Submit to main thread and wait
        future = ctx.main_executor.submit(_execute_code, code)

        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, future.result, timeout_s),
            timeout=timeout_s + 2.0,
        )

        status = result.get("status", "unknown")
        if status == "error":
            details: dict = {}
            for key in ("exception_type", "traceback", "traceback_truncated", "log_file"):
                if key in result:
                    details[key] = result[key]
            return {
                "type": "execute_code_result",
                "request_id": request_id,
                "status": "error",
                "message": result.get("message", ""),
                "error": {
                    "code": "execute_code_error",
                    "message": result.get("message", ""),
                    "details": details or None,
                },
                "data": {
                    "output": result.get("output", ""),
                },
            }

        return {
            "type": "execute_code_result",
            "request_id": request_id,
            "status": "success",
            "message": "Code executed successfully",
            "data": {
                "output": result.get("output", ""),
                "result": result.get("result"),
            },
        }

    except (asyncio.TimeoutError, TimeoutError):
        termination = await _terminate_stuck_execution(request_id, future)
        return _timeout_response(request_id, timeout_ms, termination)

    except Exception as e:
        logger.error(f"Code execution failed: {e}")
        return {
            "type": "execute_code_result",
            "request_id": request_id,
            "status": "error",
            "message": str(e),
            "error": {
                "code": "execute_code_failed",
                "message": str(e),
            },
            "data": None,
        }
