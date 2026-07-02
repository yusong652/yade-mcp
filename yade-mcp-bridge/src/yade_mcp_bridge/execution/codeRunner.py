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
    clearCurrentTask,
    clearInterrupt,
    getCurrentTask,
    getExecThread,
    holdSim,
    registerExecThread,
    requestInterrupt,
    setCurrentTask,
    unregisterExecThread,
)
from ..utils import TeeBuffer, errorResponse, okResponse
from .errors import (
    EXECUTE_CODE_TAG,
    formatExecutionError,
    isExecuteCodeFrame,
    logExecuteCodeOverflow,
)
from .termination import AsyncAbort, CycleInterrupt, injectAsyncException

logger = logging.getLogger("MCP-Bridge")

# How long we wait for the PyRunner tick to interrupt a running cycle.
_CYCLE_INTERRUPT_GRACE_S = 2.0

# How long we wait for the code to die after injecting AsyncAbort into its
# thread, before giving up as "stuck in a C call".
_TERMINATION_GRACE_S = 0.5


def _simRunning():
    """True if YADE's simulation loop is live (``O.running``)."""
    try:
        from yade import O

        return bool(O.running)
    except Exception:
        return False


def _execute(requestId, codeStr, timeoutMs):
    """Run a snippet on the pump thread, capturing stdout. Returns an internal
    status dict (success / error / terminated / interrupted).
    """
    outputBuffer = StringIO()
    oldStdout = sys.stdout

    # Record the pump thread so the timeout caller can async-inject
    # AsyncAbort to abort us.
    registerExecThread(requestId, threading.get_ident())

    try:
        terminal = sys.__stdout__ if sys.__stdout__ is not None else oldStdout
        sys.stdout = TeeBuffer(terminal, outputBuffer)

        import __main__

        execGlobals = __main__.__dict__
        execGlobals.pop("result", None)

        def _doExec():
            # eval first (bare expression returns a value); fall back to exec.
            try:
                codeObj = compile(codeStr, EXECUTE_CODE_TAG, "eval")
                return eval(codeObj, execGlobals, execGlobals)
            except SyntaxError:
                codeObj = compile(codeStr, EXECUTE_CODE_TAG, "exec")
                exec(codeObj, execGlobals, execGlobals)
                return execGlobals.get("result", None)

        if getCurrentTask() is not None or _simRunning():
            # a task is running, or user ran O.run() in console.
            # Hold the sim so that the code can read a snapshot.
            maxHoldS = timeoutMs / 1000.0 + _CYCLE_INTERRUPT_GRACE_S + _TERMINATION_GRACE_S + 1.0
            with holdSim(maxHoldS=maxHoldS):
                result = _doExec()
        else:
            result = _doExec()

        return {
            "status": "success",
            "output": outputBuffer.getvalue(),
            "result": result
            if isinstance(result, (str, int, float, bool, list, dict, type(None)))
            else str(result)
            if result is not None
            else None,
        }
    except CycleInterrupt:
        # Timed out: the PyRunner started by the cycle paused our O.run at a
        # cycle boundary and raised here. Marker → status="interrupted".
        return {"status": "interrupted", "output": outputBuffer.getvalue()}
    except AsyncAbort:
        # Timed out: terminated by async exception injection into this thread.
        return {"status": "terminated", "output": outputBuffer.getvalue()}
    except Exception as e:
        # Returned successfully: the code raised an exception of its own.
        outputText = outputBuffer.getvalue()
        # Drop the compile(eval)->compile(exec) fallback's chained-SyntaxError
        # preamble — pure plumbing.
        e.__suppress_context__ = True

        return formatExecutionError(
            e,
            outputText,
            keepFrame=isExecuteCodeFrame,
            displayPath=EXECUTE_CODE_TAG,
            overflowWriter=logExecuteCodeOverflow,
        )
    finally:
        sys.stdout = oldStdout
        clearInterrupt(requestId)
        unregisterExecThread(requestId)


def _terminateStuckExecution(requestId, future):
    """Terminate a timed-out ``execute_code`` submission."""
    # Set the interrupt flag first: if the code is inside ``O.run(wait=True)``,
    # the PyRunner tick sees the flag and pauses the cycle, so O.run returns.
    requestInterrupt(requestId)

    # Temporarily set the current task id to this request_id, so the next
    # PyRunner tick picks up the interrupt flag and O.pause()s the run.
    if getCurrentTask() is None and _simRunning():
        setCurrentTask(requestId)
        try:
            result = future.result(timeout=_CYCLE_INTERRUPT_GRACE_S)
        except concurrent.futures.TimeoutError:
            # Pause signalled, but a too-heavy step didn't reach the next cycle
            # boundary within grace. The pause is sticky, so the pump should
            # free shortly after we return.
            return {"method": "cycle_stuck", "result": None}
        finally:
            # CAS: don't wipe a task that claimed the slot mid-grace.
            if getCurrentTask() == requestId:
                clearCurrentTask()
            clearInterrupt(requestId)

        status = result.get("status") if isinstance(result, dict) else None
        method = "cycle_interrupt" if status == "interrupted" else "cycle_finished"
        return {"method": method, "result": result}

    tid = getExecThread(requestId)

    # Registry already cleared → _execute's finally ran → the pump is free.
    if tid is None:
        if future.done():
            # The code raced the timeout and settled before we could inject.
            return {"method": "finished", "result": future.result()}
        # Narrow race: registry cleared but the executor hasn't set the
        # future's result yet (it sets it after _execute returns).
        return {"method": "unsettled", "result": None}

    injectAsyncException(tid, AsyncAbort)

    try:
        result = future.result(timeout=_TERMINATION_GRACE_S)
        return {"method": "async_exc", "result": result}
    except concurrent.futures.TimeoutError:
        return {"method": "stuck_in_c", "result": None}


def _timeoutResponse(requestId, timeoutMs, termination):
    """Build the wire response for an ``execute_code`` that timed out."""
    method = termination["method"]
    result = termination.get("result")

    # Partial stdout captured before the abort. Only the settled methods carry a
    # result dict; the rest leave it None.
    output = ""
    if isinstance(result, dict):
        output = result.get("output", "") or ""

    if method == "cycle_interrupt":
        errorCode = "interrupted"
        message = (
            f"A simulation cycle (O.run) inside execute_code exceeded the "
            f"{timeoutMs}ms timeout and was paused cleanly at an iteration "
            "boundary."
        )
    elif method == "async_exc":
        errorCode = "terminated"
        message = (
            f"Execution exceeded the {timeoutMs}ms timeout and was aborted. "
            "YADE state may be partially modified by code that ran before the "
            "abort."
        )
    elif method in ("finished", "cycle_finished"):
        # The code raced the abort and completed first.
        errorCode = "terminated"
        message = (
            f"Execution exceeded the {timeoutMs}ms timeout but finished on "
            "its own before the abort landed; its result was discarded. Any "
            "state changes it made are fully in effect."
        )
    elif method == "stuck_in_c":
        errorCode = "timeout"
        message = (
            f"Execution exceeded the {timeoutMs}ms timeout; the abort "
            "exception was queued but the code has not yielded — likely "
            "blocked in a C extension."
        )
    elif method == "cycle_stuck":
        errorCode = "timeout"
        message = (
            f"A simulation cycle (O.run) exceeded the {timeoutMs}ms timeout; "
            f"a pause was requested, but the cycle is too heavy to reach an "
            f"iteration boundary within {_CYCLE_INTERRUPT_GRACE_S:.0f}s. The "
            "pause is sticky — the run stops at the next boundary."
        )
    else:  # "unsettled" — defensive: registry cleared but future not yet set.
        errorCode = "timeout"
        message = f"Execution exceeded the {timeoutMs}ms timeout."

    details = {"method": method}

    return errorResponse(
        "execute_code_result",
        requestId,
        errorCode,
        message,
        details=details,
        data={"output": output},
    )


class CodeRunner:
    """Submit ``execute_code`` snippets to the executor and block for the result."""

    def __init__(self, executor):
        self.executor = executor

    def run(self, requestId, code, timeoutMs):
        """Run ``code`` and return the full ``execute_code_result`` response."""
        try:
            # Submit to the pump and block until it resolves or times out.
            future = self.executor.submit(_execute, requestId, code, timeoutMs)
            result = future.result(timeout=timeoutMs / 1000.0)

            # ``status`` is _execute's internal marker; translate it to the envelope.
            if result.get("status") == "error":
                details = {}
                for key in ("exception_type", "traceback", "traceback_truncated", "log_file"):
                    if key in result:
                        details[key] = result[key]
                # User code raised: an execution error, not a bridge fault.
                return errorResponse(
                    "execute_code_result",
                    requestId,
                    "execution_error",
                    result.get("message", ""),
                    details=details or None,
                    data={"output": result.get("output", "")},
                )

            return okResponse(
                "execute_code_result",
                requestId,
                data={
                    "output": result.get("output", ""),
                    "result": result.get("result"),
                },
            )

        except concurrent.futures.TimeoutError:
            termination = _terminateStuckExecution(requestId, future)
            return _timeoutResponse(requestId, timeoutMs, termination)

        except Exception as e:
            # Bridge-side fault (executor submission, result plumbing) — the
            # user's code never ran or its outcome was lost. Same code the
            # transport layer uses for handler crashes.
            logger.error(f"Code execution failed: {e}")
            return errorResponse("execute_code_result", requestId, "internal_error", str(e))
