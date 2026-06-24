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
from .termination import inject_async_exception

logger = logging.getLogger("MCP-Bridge")

# Grace for the cycle-interrupt (PyRunner) path: how long we wait for O.pause()
# to land after arming the flag. The tick runs every iteration, so only a step
# longer than this can exceed it.
_CYCLE_INTERRUPT_GRACE_S = 2.0

# Grace for the thread-injection path: how long we wait for the pump thread to
# unwind after we async-inject BridgeTimeout, before giving up as "stuck in C".
_TERMINATION_GRACE_S = 0.5


def _sim_running() -> bool:
    """True if YADE's simulation loop is live (``O.running``)."""
    try:
        from yade import O

        return bool(O.running)
    except Exception:
        return False


def _run_code(request_id, code_str):
    """Run a snippet on the pump thread, capturing stdout. Returns an internal
    status dict (success / error / terminated / interrupted).
    """
    output_buffer = StringIO()
    old_stdout = sys.stdout

    # Record the pump thread so the timeout caller can async-inject
    # BridgeTimeout to abort us.
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
                code_obj = compile(code_str, "<execute_code>", "eval")
                return eval(code_obj, exec_globals, exec_globals)
            except SyntaxError:
                code_obj = compile(code_str, "<execute_code>", "exec")
                exec(code_obj, exec_globals, exec_globals)
                return exec_globals.get("result", None)

        if _sim_running():
            # A task owns the live cycle: pause it so the snippet reads a
            # consistent snapshot and does not race the task. Resumes on exit.
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
    except InterruptedError:
        # Cycle-interrupt SUCCEEDED: the tick paused our O.run at a cycle
        # boundary and _hooked_run raised here. Marker → status="interrupted".
        return {"status": "interrupted", "output": output_buffer.getvalue()}
    except BridgeTimeout:
        # Async-injection SUCCEEDED: our BridgeTimeout reached code stuck in a
        # pure-Python loop and aborted it. Return a marker, not a raise — see
        # BridgeTimeout's BaseException rationale in errors.py.
        return {"status": "terminated", "output": output_buffer.getvalue()}
    except Exception as e:
        # The agent's own code raised — a real execution error, not a bridge
        # cancellation. Format it into an agent-facing error response.
        output_text = output_buffer.getvalue()
        # Drop the compile(eval)->compile(exec) fallback's chained-SyntaxError
        # preamble — pure plumbing.
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
    """Terminate a timed-out ``execute_code`` submission.

    Returns an outcome dict for ``_timeout_response``: ``method`` (which
    cancellation outcome occurred) and ``result`` (the future's result dict,
    carried for its stdout, or None when no result was retrieved).
    """
    # Fire the flag first — cheap, helps if code is inside ``O.run(wait=True)``
    # (PyRunner tick → O.pause → O.run returns).
    request_interrupt(request_id)

    # Cycle-interrupt path: no task owns the sim yet ``O.running`` is True, so the
    # live ``O.run`` must be this execute_code's own — safe to arm ``_current_task_id``
    # (normally avoided: it would mask a concurrent task).
    if get_current_task() is None and _sim_running():
        # Arming our id makes the PyRunner tick honor the flag and O.pause() the run.
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
        # ``interrupted`` means the tick paused our O.run as intended; any other
        # status means the cycle finished on its own within the grace window.
        method = "cycle_interrupt" if status == "interrupted" else "cycle_finished"
        return {"method": method, "result": result}

    tid = get_exec_thread(request_id)

    # Registry already cleared → _run_code's finally ran → the pump is free.
    if tid is None:
        if future.done():
            # The code raced the timeout and settled before we could inject.
            return {"method": "finished", "result": future.result()}
        # Ultra-narrow window: registry cleared but the executor hasn't set the
        # future's result yet (it sets it after _run_code returns).
        return {"method": "unsettled", "result": None}

    inject_async_exception(tid, BridgeTimeout)

    try:
        result = future.result(timeout=_TERMINATION_GRACE_S)
        return {"method": "async_exc", "result": result}
    except concurrent.futures.TimeoutError:
        return {"method": "stuck_in_c", "result": None}


def _timeout_response(request_id: str, timeout_ms: int, termination: dict) -> dict:
    """Build the wire response for an ``execute_code`` that timed out.

    Each cancellation ``method`` maps to exactly one wire outcome
    (``error.code`` + message) — there is no separate resolved/unresolved axis:

    * ``cycle_interrupt`` → ``interrupted`` (a standalone O.run was paused)
    * ``async_exc`` / ``finished`` / ``cycle_finished`` → ``terminated``
    * ``stuck_in_c`` / ``cycle_stuck`` / ``unsettled`` → ``timeout``

    Messages state only what the bridge observed/did — no client-tool names or
    agent guidance. The MCP layer maps ``code`` + ``details.method`` to
    agent-facing advice (which tool to use, when to restart).
    """
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
    elif method in ("async_exc", "finished", "cycle_finished"):
        # Aborted by async exception, or the code finished on its own racing
        # the timeout — either way the pump is free.
        error_code = "terminated"
        message = (
            f"Execution exceeded the {timeout_ms}ms timeout and was aborted. "
            "YADE state may be partially modified by code that ran before the "
            "abort."
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
            f"A simulation cycle (O.run) exceeded the {timeout_ms}ms timeout; a "
            "pause was requested but the cycle did not yield within the grace "
            "period."
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
