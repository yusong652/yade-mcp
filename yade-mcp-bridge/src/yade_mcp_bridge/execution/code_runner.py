# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Synchronous code executor for the bridge.

``CodeRunner`` runs an ``execute_code`` snippet on the pump thread and
returns the wire response. Eval-then-exec semantics: a bare expression is
evaluated and its value returned, otherwise the snippet is exec'd against
the persistent ``__main__`` namespace, stdout captured, last value returned.
"""

import concurrent.futures
import logging
import os
import sys
import threading
import time
from io import StringIO

from ..paths import LOGS_DIR
from ..runtime.signals import (
    clear_current_task,
    clear_interrupt,
    get_current_task,
    get_exec_thread,
    register_exec_thread,
    request_interrupt,
    set_current_task,
    sim_paused_window,
    unregister_exec_thread,
)
from ..utils import TeeBuffer, error_response, ok_response
from .errors import BridgeTimeout, format_execution_error
from .termination import fire_async_exception, is_safe_to_async_raise

logger = logging.getLogger("MCP-Bridge")

# Grace for the pump thread to unwind after we inject ``BridgeTimeout``. A
# pure-Python loop aborts within milliseconds; 0.5s covers worst-case user
# ``finally`` blocks without letting "stuck in C" cases stall the response.
_TERMINATION_GRACE_S = 0.5

# Grace for the cycle-interrupt path after arming the flag. ``_mcp_pyrunner_tick``
# runs every iteration, so ``O.pause()`` lands almost immediately; only a single
# step longer than this can exceed it. Slightly above ``_TERMINATION_GRACE_S`` to
# absorb a heavy step before the cycle yields.
_CYCLE_INTERRUPT_GRACE_S = 2.0


def _sim_running() -> bool:
    """True if YADE's simulation loop is live (``O.running``).

    Lazy import so this module stays importable without YADE (unit/protocol
    tests), where the cycle-interrupt path is simply never taken.
    """
    try:
        from yade import O

        return bool(O.running)
    except Exception:
        return False


def _run_code(request_id, code_str):
    """Run a snippet on the pump thread, capturing stdout. Returns an internal
    status dict (success / error / terminated / interrupted).

    Registers the running thread id so a timeout in the caller can inject
    ``PyThreadState_SetAsyncExc(BridgeTimeout)``. The ``except BridgeTimeout``
    branch is LOAD-BEARING: ``BridgeTimeout`` is a ``BaseException`` and must
    not escape, or it slips past ``SerialExecutor.run_next``'s
    ``except Exception`` and kills the pump thread permanently.

    Deliberately does NOT touch ``_current_task_id``: it is read by
    ``_mcp_pyrunner_tick``, and overwriting it with our ``request_id`` would
    make a later ``_terminate_stuck_execution`` flag look like a task interrupt,
    wrongly pausing a concurrent task. execute_code cancels via async_exc.
    """
    output_buffer = StringIO()
    old_stdout = sys.stdout

    register_exec_thread(request_id, threading.get_ident())

    try:
        # Redirect inside the try so ``finally``'s restore is always paired
        # with a successful assignment.
        terminal = sys.__stdout__ if sys.__stdout__ is not None else old_stdout
        sys.stdout = TeeBuffer(terminal, output_buffer)

        import __main__

        exec_globals = __main__.__dict__
        exec_globals.pop("result", None)

        def _do_exec():
            # eval first (bare expression returns a value); fall back to exec.
            try:
                code_obj = compile(code_str, "<execute_code>", "eval")
                return eval(code_obj, exec_globals, exec_globals)
            except SyntaxError:
                code_obj = compile(code_str, "<execute_code>", "exec")
                exec(code_obj, exec_globals, exec_globals)
                return exec_globals.get("result", None)

        if _sim_running():
            # A task owns the live cycle: freeze it at an engine boundary so the
            # snippet reads a consistent snapshot and any mutation does not race
            # the cycle. Still runs on the pump thread, so a timeout can async-
            # abort it. Always resumes on exit (the window's finally).
            with sim_paused_window():
                result = _do_exec()
        else:
            result = _do_exec()

        return {
            "status": "success",
            "output": output_buffer.getvalue(),
            "result": result
            if isinstance(result, (str, int, float, bool, list, dict, type(None)))
            else str(result)
            if result is not None
            else None,
        }
    except BridgeTimeout:
        # Bridge-initiated termination. Return a marker (not a raise) so the
        # caller reports status="terminated"; must NOT escape — see docstring.
        return {"status": "terminated", "output": output_buffer.getvalue()}
    except InterruptedError:
        # Cycle-interrupt: ``_terminate_stuck_execution`` armed our request_id,
        # the tick paused our own ``O.run``, and ``_hooked_run`` raised here.
        # Marker → status="interrupted". Like BridgeTimeout, caught here so the
        # future resolves and the pump frees.
        return {"status": "interrupted", "output": output_buffer.getvalue()}
    except Exception as e:
        output_text = output_buffer.getvalue()
        # Suppress the compile(eval)->compile(exec) fallback's chained-exception
        # "During handling of the above..." preamble — pure plumbing.
        e.__suppress_context__ = True

        def _overflow_writer(full_tb: str) -> str:
            # Only on truncation. Fresh timestamped file per error: execute_code
            # runs synchronously one-at-a-time, so a rolling file would race a
            # concurrent call.
            os.makedirs(LOGS_DIR, exist_ok=True)
            path = os.path.join(LOGS_DIR, f"exec_code_error_{int(time.time() * 1000)}.log")
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


def _terminate_stuck_execution(request_id: str, future) -> dict:
    """Best-effort cancellation of a timed-out ``execute_code`` submission.

    Returns an outcome dict consumed by ``_timeout_response``: ``resolved``
    (did the pump settle the future within the grace), ``method`` (which
    cancellation path ran), ``reason`` (set on ``flag_only``), and ``result``
    (the future's result dict when resolved, else None).
    """
    # Fire the flag first — cheap, helps if code is inside ``O.run(wait=True)``
    # (PyRunner tick → O.pause → O.run returns).
    request_interrupt(request_id)

    # Cycle-interrupt path: no task owns the sim yet ``O.running`` is True, so the
    # live ``O.run`` must be this execute_code's own. Arm ``_current_task_id`` with
    # our request_id (normally we don't — it would mask a concurrent task) so the
    # PyRunner tick honors the flag: ``O.pause()`` → ``O.run`` returns →
    # ``_hooked_run`` raises ``InterruptedError`` → reported as ``interrupted``.
    # CAS-clears on exit so a task that claimed the slot mid-grace survives.
    # async_exc is skipped here: a C++ ``O.run`` released the GIL, so an injected
    # ``BridgeTimeout`` could not fire until the cycle returns anyway.
    if get_current_task() is None and _sim_running():
        set_current_task(request_id)
        try:
            result = future.result(timeout=_CYCLE_INTERRUPT_GRACE_S)
        except concurrent.futures.TimeoutError:
            # O.pause didn't free O.run within grace (a single step longer than
            # the grace, or no tick yet). The O.pause is sticky, so the pump will
            # likely free shortly after we return.
            return {"resolved": False, "method": "cycle_stuck", "result": None}
        finally:
            # CAS: don't wipe a task that claimed the slot mid-grace.
            if get_current_task() == request_id:
                clear_current_task()
            clear_interrupt(request_id)

        status = result.get("status") if isinstance(result, dict) else None
        # ``interrupted`` means the tick paused our O.run as intended; any other
        # status means the cycle finished on its own within the grace window.
        method = "cycle_interrupt" if status == "interrupted" else "cycle_self"
        return {"resolved": True, "method": method, "result": result}

    tid = get_exec_thread(request_id)

    # Registry already cleared → _run_code's finally ran → the pump is free.
    if tid is None:
        if future.done():
            return {"resolved": True, "method": "self", "result": future.result()}
        return {"resolved": False, "method": "self", "result": None}

    safe, reason = is_safe_to_async_raise(tid)
    if not safe:
        # Can't safely inject (Dummy-N nested case, or Qt main thread in GUI
        # mode). Fall back to flag-only cancellation.
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
        result = future.result(timeout=_TERMINATION_GRACE_S)
        return {"resolved": True, "method": "async_exc", "result": result}
    except concurrent.futures.TimeoutError:
        return {"resolved": False, "method": "stuck_in_c", "result": None}


def _timeout_response(request_id: str, timeout_ms: int, termination: dict) -> dict:
    """Build the wire response for an ``execute_code`` that timed out.

    * ``resolved=True`` → status ``"terminated"``: pump free, but YADE state may
      be partially modified by whatever the user code wrote before the abort.
    * ``resolved=False`` → status ``"timeout"``: cancellation couldn't complete.
    * ``method == "cycle_interrupt"`` → status ``"interrupted"``: a standalone
      ``O.run`` was cleanly paused, with a pull-back to ``yade_execute_task``.
    """
    resolved = termination["resolved"]
    method = termination["method"]
    result = termination.get("result")

    # Output captured up to the abort point (present on async_exc / self paths).
    output = ""
    if isinstance(result, dict):
        output = result.get("output", "") or ""

    if method == "cycle_interrupt":
        # A standalone execute_code ran an O.run cycle past its timeout; the
        # bridge paused it cleanly. Pull the agent back to the task tool.
        error_code = "interrupted"
        message = (
            f"Ran a simulation cycle (O.run) inside execute_code that "
            f"exceeded the {timeout_ms}ms timeout; it was interrupted and "
            "the simulation paused cleanly at an iteration boundary. For "
            "long simulations or solving to equilibrium, use "
            "yade_execute_task — it tracks progress and can be cleanly "
            "stopped via yade_interrupt_task."
        )
    elif resolved:
        error_code = "terminated"
        message = (
            f"Execution timed out after {timeout_ms}ms and was aborted. "
            "YADE state may be partially modified by the aborted code; "
            "inspect via yade_execute_code before retrying."
        )
    else:
        error_code = "timeout"
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
        elif method == "cycle_stuck":
            message = (
                f"Execution timed out after {timeout_ms}ms while running a "
                "simulation cycle (O.run). The bridge requested a pause "
                "but the cycle did not yield within the grace period; it "
                "should stop shortly. Use yade_execute_task for long "
                "simulations; restart the bridge if execute_code stays "
                "unresponsive."
            )
        else:
            # self without resolved — defensive.
            message = f"Execution timed out after {timeout_ms}ms."

    details = {"method": method}
    if "reason" in termination:
        details["reason"] = termination["reason"]

    return error_response(
        "execute_code_result",
        request_id,
        error_code,
        message,
        details=details,
        data={"output": output},
    )


class CodeRunner:
    """Run ``execute_code`` snippets synchronously on the pump thread.

    Submits each snippet to the serial executor and blocks the calling request
    thread until it resolves or the timeout fires.
    """

    def __init__(self, executor):
        self.executor = executor

    def execute(self, request_id, code, timeout_ms):
        """Run ``code`` and return the full ``execute_code_result`` response."""
        try:
            # Submit to the pump and block until it resolves or times out.
            future = self.executor.submit(_run_code, request_id, code)
            result = future.result(timeout=timeout_ms / 1000.0)

            # ``status`` is _run_code's internal marker; translate it to the envelope.
            if result.get("status") == "error":
                details: dict = {}
                for key in ("exception_type", "traceback", "traceback_truncated", "log_file"):
                    if key in result:
                        details[key] = result[key]
                # User code raised: an execution error, not a bridge fault.
                return error_response(
                    "execute_code_result",
                    request_id,
                    "execution_error",
                    result.get("message", ""),
                    details=details or None,
                    data={"output": result.get("output", "")},
                )

            return ok_response(
                "execute_code_result",
                request_id,
                data={
                    "output": result.get("output", ""),
                    "result": result.get("result"),
                },
            )

        except concurrent.futures.TimeoutError:
            termination = _terminate_stuck_execution(request_id, future)
            return _timeout_response(request_id, timeout_ms, termination)

        except Exception as e:
            # Bridge-side fault (executor submission, result plumbing) — the
            # user's code never ran or its outcome was lost. Same code the
            # transport layer uses for handler crashes.
            logger.error(f"Code execution failed: {e}")
            return error_response("execute_code_result", request_id, "internal_error", str(e))
