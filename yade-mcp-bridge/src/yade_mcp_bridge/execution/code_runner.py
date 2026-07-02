# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Synchronous code executor for the bridge.

``CodeRunner`` submits an ``execute_code`` snippet to the executor queue and
blocks until it produces the response to send back to the client.
"""

import concurrent.futures
import logging
import sys
import threading
from io import StringIO

from ..runtime.signals import (
    clear_current_task,
    clear_interrupt,
    get_current_task,
    get_exec_thread,
    hold_sim,
    register_exec_thread,
    request_interrupt,
    set_current_task,
    unregister_exec_thread,
)
from ..utils import TeeBuffer, error_response, ok_response
from .errors import (
    EXECUTE_CODE_TAG,
    format_execution_error,
    is_execute_code_frame,
    log_execute_code_overflow,
)
from .termination import AsyncAbort, CycleInterrupt, inject_async_exception

logger = logging.getLogger("MCP-Bridge")

# How long we wait for the PyRunner tick to interrupt a running cycle.
_CYCLE_INTERRUPT_GRACE_S = 2.0

# How long we wait for the code to die after injecting AsyncAbort into its
# thread, before giving up as "stuck in a C call".
_TERMINATION_GRACE_S = 0.5


def _sim_running():
    """True if YADE's simulation loop is live (``O.running``)."""
    try:
        from yade import O

        return bool(O.running)
    except Exception:
        return False


def _execute(request_id, code_str, timeout_ms):
    """Run a snippet on the pump thread, capturing stdout. Returns an internal
    status dict (success / error / terminated / interrupted).
    """
    output_buffer = StringIO()
    old_stdout = sys.stdout

    # Record the pump thread so the timeout caller can async-inject
    # AsyncAbort to abort us.
    register_exec_thread(request_id, threading.get_ident())

    try:
        terminal = sys.__stdout__ if sys.__stdout__ is not None else old_stdout
        sys.stdout = TeeBuffer(terminal, output_buffer)

        import __main__

        exec_globals = __main__.__dict__
        exec_globals.pop("result", None)

        def _do_exec():
            # eval first (bare expression returns a value); fall back to exec.
            try:
                code_obj = compile(code_str, EXECUTE_CODE_TAG, "eval")
                return eval(code_obj, exec_globals, exec_globals)
            except SyntaxError:
                code_obj = compile(code_str, EXECUTE_CODE_TAG, "exec")
                exec(code_obj, exec_globals, exec_globals)
                return exec_globals.get("result", None)

        if get_current_task() is not None or _sim_running():
            # a task is running, or user ran O.run() in console.
            # Hold the sim so that the code can read a snapshot.
            max_hold_s = timeout_ms / 1000.0 + _CYCLE_INTERRUPT_GRACE_S + _TERMINATION_GRACE_S + 1.0
            with hold_sim(max_hold_s=max_hold_s):
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
    except CycleInterrupt:
        # Timed out: the PyRunner started by the cycle paused our O.run at a
        # cycle boundary and raised here. Marker → status="interrupted".
        return {"status": "interrupted", "output": output_buffer.getvalue()}
    except AsyncAbort:
        # Timed out: terminated by async exception injection into this thread.
        return {"status": "terminated", "output": output_buffer.getvalue()}
    except Exception as e:
        # Returned successfully: the code raised an exception of its own.
        output_text = output_buffer.getvalue()
        # Drop the compile(eval)->compile(exec) fallback's chained-SyntaxError
        # preamble — pure plumbing.
        e.__suppress_context__ = True

        return format_execution_error(
            e,
            output_text,
            keep_frame=is_execute_code_frame,
            display_path=EXECUTE_CODE_TAG,
            overflow_writer=log_execute_code_overflow,
        )
    finally:
        sys.stdout = old_stdout
        clear_interrupt(request_id)
        unregister_exec_thread(request_id)


def _terminate_stuck_execution(request_id, future):
    """Terminate a timed-out ``execute_code`` submission."""
    # Set the interrupt flag first: if the code is inside ``O.run(wait=True)``,
    # the PyRunner tick sees the flag and pauses the cycle, so O.run returns.
    request_interrupt(request_id)

    # Temporarily set the current task id to this request_id, so the next
    # PyRunner tick picks up the interrupt flag and O.pause()s the run.
    if get_current_task() is None and _sim_running():
        set_current_task(request_id)
        try:
            result = future.result(timeout=_CYCLE_INTERRUPT_GRACE_S)
        except concurrent.futures.TimeoutError:
            # Pause signalled, but a too-heavy step didn't reach the next cycle
            # boundary within grace. The pause is sticky, so the pump should
            # free shortly after we return.
            return {"method": "cycle_stuck", "result": None}
        finally:
            # CAS: don't wipe a task that claimed the slot mid-grace.
            if get_current_task() == request_id:
                clear_current_task()
            clear_interrupt(request_id)

        status = result.get("status") if isinstance(result, dict) else None
        method = "cycle_interrupt" if status == "interrupted" else "cycle_finished"
        return {"method": method, "result": result}

    tid = get_exec_thread(request_id)

    # Registry already cleared → _execute's finally ran → the pump is free.
    if tid is None:
        if future.done():
            # The code raced the timeout and settled before we could inject.
            return {"method": "finished", "result": future.result()}
        # Narrow race: registry cleared but the executor hasn't set the
        # future's result yet (it sets it after _execute returns).
        return {"method": "unsettled", "result": None}

    inject_async_exception(tid, AsyncAbort)

    try:
        result = future.result(timeout=_TERMINATION_GRACE_S)
        return {"method": "async_exc", "result": result}
    except concurrent.futures.TimeoutError:
        return {"method": "stuck_in_c", "result": None}


def _timeout_response(request_id, timeout_ms, termination):
    """Build the wire response for an ``execute_code`` that timed out."""
    method = termination["method"]
    result = termination.get("result")

    # Partial stdout captured before the abort. Only the settled methods carry a
    # result dict; the rest leave it None.
    output = ""
    if isinstance(result, dict):
        output = result.get("output", "") or ""

    if method == "cycle_interrupt":
        error_code = "interrupted"
        message = (
            f"A simulation cycle (O.run) inside execute_code exceeded the "
            f"{timeout_ms}ms timeout and was paused cleanly at an iteration "
            "boundary."
        )
    elif method == "async_exc":
        error_code = "terminated"
        message = (
            f"Execution exceeded the {timeout_ms}ms timeout and was aborted. "
            "YADE state may be partially modified by code that ran before the "
            "abort."
        )
    elif method in ("finished", "cycle_finished"):
        # The code raced the abort and completed first.
        error_code = "terminated"
        message = (
            f"Execution exceeded the {timeout_ms}ms timeout but finished on "
            "its own before the abort landed; its result was discarded. Any "
            "state changes it made are fully in effect."
        )
    elif method == "stuck_in_c":
        error_code = "timeout"
        message = (
            f"Execution exceeded the {timeout_ms}ms timeout; the abort "
            "exception was queued but the code has not yielded — likely "
            "blocked in a C extension."
        )
    elif method == "cycle_stuck":
        error_code = "timeout"
        message = (
            f"A simulation cycle (O.run) exceeded the {timeout_ms}ms timeout; "
            f"a pause was requested, but the cycle is too heavy to reach an "
            f"iteration boundary within {_CYCLE_INTERRUPT_GRACE_S:.0f}s. The "
            "pause is sticky — the run stops at the next boundary."
        )
    else:  # "unsettled" — defensive: registry cleared but future not yet set.
        error_code = "timeout"
        message = f"Execution exceeded the {timeout_ms}ms timeout."

    details = {"method": method}

    return error_response(
        "execute_code_result",
        request_id,
        error_code,
        message,
        details=details,
        data={"output": output},
    )


class CodeRunner:
    """Submit ``execute_code`` snippets to the executor and block for the result."""

    def __init__(self, executor):
        self.executor = executor

    def run(self, request_id, code, timeout_ms):
        """Run ``code`` and return the full ``execute_code_result`` response."""
        try:
            # Submit to the pump and block until it resolves or times out.
            future = self.executor.submit(_execute, request_id, code, timeout_ms)
            result = future.result(timeout=timeout_ms / 1000.0)

            # ``status`` is _execute's internal marker; translate it to the envelope.
            if result.get("status") == "error":
                details = {}
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
