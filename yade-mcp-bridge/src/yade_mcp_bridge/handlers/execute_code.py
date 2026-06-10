"""Execute code message handler for YADE bridge.

Handles synchronous code snippet execution via main thread queue.
"""

import concurrent.futures
import logging
import os
import sys
import threading
import time
from io import StringIO

from ..execution.errors import BridgeTimeout, format_execution_error
from ..execution.termination import fire_async_exception, is_safe_to_async_raise
from ..signals import (
    clear_current_task,
    clear_interrupt,
    get_exec_thread,
    peek_current_task,
    register_exec_thread,
    request_interrupt,
    set_current_task,
    sim_paused_window,
    unregister_exec_thread,
)
from ..utils import TeeBuffer, error_response, ok_response
from .helpers import require_field

logger = logging.getLogger("YADE-Bridge")

# How long to wait for the pump thread to unwind after we inject
# ``BridgeTimeout``. A pure-Python loop aborts within a handful of
# bytecode instructions (milliseconds); 0.5s covers worst-case
# user-code ``finally`` blocks without letting "stuck in C" cases
# stall the handler response.
_TERMINATION_GRACE_S = 0.5

# How long to wait for the cycle-interrupt path to settle after we arm
# the interrupt flag. ``_mcp_pyrunner_tick`` runs every iteration
# (iterPeriod=1), so ``O.pause()`` lands almost immediately — the only
# way to exceed this is a single simulation step longer than the grace.
# Slightly more generous than ``_TERMINATION_GRACE_S`` to absorb a heavy
# step before the cycle yields.
_CYCLE_INTERRUPT_GRACE_S = 2.0


def _sim_running() -> bool:
    """True if YADE's simulation loop is live (``O.running``).

    Lazy import so this handler module stays importable without YADE
    (unit/protocol tests, non-YADE bridges) — where the cycle-interrupt
    path below is simply never taken.
    """
    try:
        from yade import O

        return bool(O.running)
    except Exception:
        return False


def _terminate_stuck_execution(request_id: str, future) -> dict:
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

    When the stuck code is a standalone ``execute_code`` blocked inside
    its own ``O.run`` cycle, the ``cycle_interrupt`` / ``cycle_stuck``
    methods are used instead (see the None-gate below).
    """
    # Always fire the flag first — cheap, helps if code is inside
    # ``O.run(wait=True)`` (PyRunner tick → O.pause → O.run returns).
    request_interrupt(request_id)

    # Cycle-interrupt path. When no task owns the sim (``peek_current_task``
    # is None) yet ``O.running`` is True, the live ``O.run`` must be this
    # execute_code's own — so it is safe to arm ``_current_task_id`` with
    # our request_id. ``_mcp_pyrunner_tick``'s no-arg interrupt check then
    # honors the flag fired above: ``O.pause()`` at the next tick →
    # ``O.run`` returns → ``_hooked_run`` raises ``InterruptedError``,
    # which ``_execute_code`` catches and reports as ``interrupted``.
    #
    # We deliberately do NOT arm ``_current_task_id`` during normal
    # execute_code — it would mask a concurrent task (see ``_execute_code``
    # docstring and ``test_execute_code_does_not_clobber_current_task_id``).
    # This None-gate fires only at timeout AND only when no task owns the
    # sim, and CAS-clears on the way out so a task that claimed the slot
    # mid-grace is never wiped.
    #
    # async_exc is intentionally NOT used on this path: a C++ ``O.run``
    # has released the GIL, so an injected ``BridgeTimeout`` could not fire
    # until the cycle returns anyway. The flag/O.pause IS the mechanism;
    # skipping async_exc keeps the reported status deterministic.
    if peek_current_task() is None and _sim_running():
        set_current_task(request_id)
        try:
            result = future.result(timeout=_CYCLE_INTERRUPT_GRACE_S)
        except concurrent.futures.TimeoutError:
            # O.pause didn't free O.run within grace (a single step longer
            # than the grace, or no tick fired yet). The issued O.pause is
            # sticky, so the pump will likely free shortly after we return.
            return {"resolved": False, "method": "cycle_stuck", "result": None}
        finally:
            # CAS: don't wipe a task that claimed the slot mid-grace.
            if peek_current_task() == request_id:
                clear_current_task()
            clear_interrupt(request_id)

        status = result.get("status") if isinstance(result, dict) else None
        # ``interrupted`` means the tick paused our O.run as intended.
        # Any other resolved status means the cycle finished on its own
        # within the grace window — report it like the async_exc abort.
        method = "cycle_interrupt" if status == "interrupted" else "cycle_self"
        return {"resolved": True, "method": method, "result": result}

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
        result = future.result(timeout=_TERMINATION_GRACE_S)
        return {"resolved": True, "method": "async_exc", "result": result}
    except concurrent.futures.TimeoutError:
        return {"resolved": False, "method": "stuck_in_c", "result": None}


def _timeout_response(request_id: str, timeout_ms: int, termination: dict) -> dict:
    """Build the wire response for an ``execute_code`` that timed out.

    * ``resolved=True`` → status ``"terminated"``: pump thread is free,
      but YADE state may be partially modified by whatever the user
      code wrote before the abort.
    * ``resolved=False`` → status ``"timeout"``: cancellation couldn't
      complete; pump may still be blocked.

    The cycle-interrupt path is distinct: ``method == "cycle_interrupt"``
    → status ``"interrupted"`` (a standalone ``O.run`` was cleanly paused
    at an iteration boundary) with a pull-back to ``yade_execute_task``.
    """
    resolved = termination["resolved"]
    method = termination["method"]
    result = termination.get("result")

    # Output captured up to the abort point (present on async_exc path
    # and on self-resolve path; empty otherwise).
    output = ""
    if isinstance(result, dict):
        output = result.get("output", "") or ""

    if method == "cycle_interrupt":
        # A standalone execute_code ran an O.run cycle past its timeout;
        # the bridge paused it cleanly at an iteration boundary. Pull the
        # agent back to the task tool, which is built for long runs.
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
            # self / self_untracked without resolved — defensive.
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


def handle_execute_code(ctx, data):
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

                def _do_exec():
                    # eval first (so a bare expression returns a value);
                    # fall back to exec for statements.
                    try:
                        code_obj = compile(code_str, "<execute_code>", "eval")
                        return eval(code_obj, exec_globals, exec_globals)
                    except SyntaxError:
                        code_obj = compile(code_str, "<execute_code>", "exec")
                        exec(code_obj, exec_globals, exec_globals)
                        return exec_globals.get("result", None)

                if _sim_running():
                    # A task owns the live cycle. Freeze it at an engine
                    # boundary for the duration of this snippet so the read
                    # is a consistent snapshot and any mutation does not race
                    # the cycle. The snippet still runs here on the pump
                    # thread, so a timeout can still async-abort it. Always
                    # resumes on exit (sim_paused_window's finally), even if
                    # the snippet raises.
                    with sim_paused_window():
                        result = _do_exec()
                else:
                    result = _do_exec()

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
            except InterruptedError:
                # Cycle-interrupt path. ``_terminate_stuck_execution``
                # armed ``_current_task_id`` with this request's id, so
                # ``_mcp_pyrunner_tick`` paused this snippet's own
                # ``O.run`` and ``_hooked_run`` raised ``InterruptedError``
                # here. Report a marker so the handler surfaces
                # status="interrupted" (clean cycle-boundary pause +
                # pull-back to yade_execute_task), not a user error. Like
                # ``BridgeTimeout``, this must be caught here so the future
                # resolves and the pump frees.
                return {
                    "status": "interrupted",
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

        # Submit to the main thread and block THIS request thread until it
        # resolves or the timeout fires. ThreadingHTTPServer serves each
        # request on its own thread, so blocking here never stalls other
        # requests; ``future.result`` parks on a Condition that releases the
        # GIL while it waits.
        future = ctx.main_executor.submit(_execute_code, code)
        result = future.result(timeout=timeout_s)

        # ``result["status"]`` here is the INTERNAL future-result marker
        # from ``_execute_code`` (success / error), not a wire field — the
        # wire envelope is success/failure via ``ok`` + ``error.code``.
        if result.get("status") == "error":
            details: dict = {}
            for key in ("exception_type", "traceback", "traceback_truncated", "log_file"):
                if key in result:
                    details[key] = result[key]
            # The user's code raised: an execution error, not a bridge
            # fault. details carries the exception identity/traceback,
            # data the stdout produced before the raise.
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
        return error_response(
            "execute_code_result",
            request_id,
            "internal_error",
            str(e),
        )
